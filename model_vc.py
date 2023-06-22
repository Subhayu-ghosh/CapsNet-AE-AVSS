from itertools import cycle
import librosa
import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import itertools

from tensorboardX import SummaryWriter
from torch.optim.optimizer import Optimizer
from audioUtils.audio import inv_preemphasis, preemphasis
from data.Sample_dataset import pad_seq
from saveWav import mel2wav
from audioUtils.hparams import hparams
from model_video import VideoGenerator, STAGE2_G
from audioUtils import audio
from vocoder.models.fatchord_version import WaveRNN


_inv_mel_basis = np.linalg.pinv(audio._build_mel_basis(hparams))
mel_basis = librosa.filters.mel(hparams.sample_rate, hparams.n_fft, n_mels=40)

class LinearNorm(torch.nn.Module):
    def __init__(self, in_dim, out_dim, bias=True, w_init_gain='linear'):
        super(LinearNorm, self).__init__()
        self.linear_layer = torch.nn.Linear(in_dim, out_dim, bias=bias)

        torch.nn.init.xavier_uniform_(
            self.linear_layer.weight,
            gain=torch.nn.init.calculate_gain(w_init_gain))

    def forward(self, x):
        return self.linear_layer(x)

class RAdam(Optimizer):

    def __init__(self, params, lr=1e-3, betas=(0.9, 0.999), eps=1e-8, weight_decay=0, degenerated_to_sgd=False):
        if not 0.0 <= lr:
            raise ValueError("Invalid learning rate: {}".format(lr))
        if not 0.0 <= eps:
            raise ValueError("Invalid epsilon value: {}".format(eps))
        if not 0.0 <= betas[0] < 1.0:
            raise ValueError("Invalid beta parameter at index 0: {}".format(betas[0]))
        if not 0.0 <= betas[1] < 1.0:
            raise ValueError("Invalid beta parameter at index 1: {}".format(betas[1]))
        
        self.degenerated_to_sgd = degenerated_to_sgd
        if isinstance(params, (list, tuple)) and len(params) > 0 and isinstance(params[0], dict):
            for param in params:
                if 'betas' in param and (param['betas'][0] != betas[0] or param['betas'][1] != betas[1]):
                    param['buffer'] = [[None, None, None] for _ in range(10)]
        defaults = dict(lr=lr, betas=betas, eps=eps, weight_decay=weight_decay, buffer=[[None, None, None] for _ in range(10)])
        super(RAdam, self).__init__(params, defaults)

    def __setstate__(self, state):
        super(RAdam, self).__setstate__(state)

    def step(self, closure=None):

        loss = None
        if closure is not None:
            loss = closure()

        for group in self.param_groups:

            for p in group['params']:
                if p.grad is None:
                    continue
                grad = p.grad.data.float()
                if grad.is_sparse:
                    raise RuntimeError('RAdam does not support sparse gradients')

                p_data_fp32 = p.data.float()

                state = self.state[p]

                if len(state) == 0:
                    state['step'] = 0
                    state['exp_avg'] = torch.zeros_like(p_data_fp32)
                    state['exp_avg_sq'] = torch.zeros_like(p_data_fp32)
                else:
                    state['exp_avg'] = state['exp_avg'].type_as(p_data_fp32)
                    state['exp_avg_sq'] = state['exp_avg_sq'].type_as(p_data_fp32)

                exp_avg, exp_avg_sq = state['exp_avg'], state['exp_avg_sq']
                beta1, beta2 = group['betas']

                exp_avg_sq.mul_(beta2).addcmul_(1 - beta2, grad, grad)
                exp_avg.mul_(beta1).add_(1 - beta1, grad)

                state['step'] += 1
                buffered = group['buffer'][int(state['step'] % 10)]
                if state['step'] == buffered[0]:
                    N_sma, step_size = buffered[1], buffered[2]
                else:
                    buffered[0] = state['step']
                    beta2_t = beta2 ** state['step']
                    N_sma_max = 2 / (1 - beta2) - 1
                    N_sma = N_sma_max - 2 * state['step'] * beta2_t / (1 - beta2_t)
                    buffered[1] = N_sma

                    # more conservative since it's an approximated value
                    if N_sma >= 5:
                        step_size = math.sqrt((1 - beta2_t) * (N_sma - 4) / (N_sma_max - 4) * (N_sma - 2) / N_sma * N_sma_max / (N_sma_max - 2)) / (1 - beta1 ** state['step'])
                    elif self.degenerated_to_sgd:
                        step_size = 1.0 / (1 - beta1 ** state['step'])
                    else:
                        step_size = -1
                    buffered[2] = step_size

                # more conservative since it's an approximated value
                if N_sma >= 5:
                    if group['weight_decay'] != 0:
                        p_data_fp32.add_(-group['weight_decay'] * group['lr'], p_data_fp32)
                    denom = exp_avg_sq.sqrt().add_(group['eps'])
                    p_data_fp32.addcdiv_(-step_size * group['lr'], exp_avg, denom)
                    p.data.copy_(p_data_fp32)
                elif step_size > 0:
                    if group['weight_decay'] != 0:
                        p_data_fp32.add_(-group['weight_decay'] * group['lr'], p_data_fp32)
                    p_data_fp32.add_(-step_size * group['lr'], exp_avg)
                    p.data.copy_(p_data_fp32)

        return loss


class ConvNorm(torch.nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size=1, stride=1,
                 padding=None, dilation=1, bias=True, w_init_gain='linear'):
        super(ConvNorm, self).__init__()
        if padding is None:
            assert(kernel_size % 2 == 1)
            padding = int(dilation * (kernel_size - 1) / 2)

        self.conv = torch.nn.Conv1d(in_channels, out_channels,
                                    kernel_size=kernel_size, stride=stride,
                                    padding=padding, dilation=dilation,
                                    bias=bias)

        torch.nn.init.xavier_uniform_(
            self.conv.weight, gain=torch.nn.init.calculate_gain(w_init_gain))

    def forward(self, signal):
        conv_signal = self.conv(signal)
        return conv_signal

class MyEncoder(nn.Module):
    '''Encoder without speaker embedding'''

    def __init__(self, dim_neck, freq, num_mel=80):
        super(MyEncoder, self).__init__()
        self.dim_neck = dim_neck
        self.freq = freq

        convolutions = []
        for i in range(3):
            conv_layer = nn.Sequential(
                ConvNorm(num_mel if i == 0 else 512,
                         512,
                         kernel_size=5, stride=1,
                         padding=2,
                         dilation=1, w_init_gain='relu'),
                nn.BatchNorm1d(512))
            convolutions.append(conv_layer)
        self.convolutions = nn.ModuleList(convolutions)

        self.lstm = nn.LSTM(512, dim_neck, 2, batch_first=True, bidirectional=True)

    def forward(self, x, return_unsample=False):
        # (B, T, n_mel)
        x = x.squeeze(1).transpose(2, 1)

        for conv in self.convolutions:
            x = F.relu(conv(x))
        x = x.transpose(1, 2)

        self.lstm.flatten_parameters()
        outputs, _ = self.lstm(x)
        out_forward = outputs[:, :, :self.dim_neck]
        out_backward = outputs[:, :, self.dim_neck:]

        codes = []
        for i in range(0, outputs.size(1), self.freq):
            codes.append(torch.cat((out_forward[:, i + self.freq - 1, :], out_backward[:, i, :]), dim=-1))
        if return_unsample:
            return codes, outputs
        return codes


class Decoder(nn.Module):
    """Decoder module:
    """
    def __init__(self, dim_neck, dim_emb, dim_pre, num_mel=80):
        super(Decoder, self).__init__()
        
        self.lstm1 = nn.LSTM(dim_neck*2+dim_emb, dim_pre, 1, batch_first=True)
        
        convolutions = []
        for i in range(3):
            conv_layer = nn.Sequential(
                ConvNorm(dim_pre,
                         dim_pre,
                         kernel_size=5, stride=1,
                         padding=2,
                         dilation=1, w_init_gain='relu'),
                nn.BatchNorm1d(dim_pre))
            convolutions.append(conv_layer)
        self.convolutions = nn.ModuleList(convolutions)
        
        self.lstm2 = nn.LSTM(dim_pre, 1024, 2, batch_first=True)
        
        self.linear_projection = LinearNorm(1024, num_mel)

    def forward(self, x):

        x, _ = self.lstm1(x)
        x = x.transpose(1, 2)
        
        for conv in self.convolutions:
            x = F.relu(conv(x))
        x = x.transpose(1, 2)
        
        outputs, _ = self.lstm2(x)
        
        decoder_output = self.linear_projection(outputs)

        return decoder_output   

    
class Postnet(nn.Module):
    """Postnet
        - Five 1-d convolution with 512 channels and kernel size 5
    """

    def __init__(self, num_mel=80):
        super(Postnet, self).__init__()
        self.convolutions = nn.ModuleList()

        self.convolutions.append(
            nn.Sequential(
                ConvNorm(num_mel, 512,
                         kernel_size=5, stride=1,
                         padding=2,
                         dilation=1, w_init_gain='tanh'),
                nn.BatchNorm1d(512))
        )

        for i in range(1, 5 - 1):
            self.convolutions.append(
                nn.Sequential(
                    ConvNorm(512,
                             512,
                             kernel_size=5, stride=1,
                             padding=2,
                             dilation=1, w_init_gain='tanh'),
                    nn.BatchNorm1d(512))
            )

        self.convolutions.append(
            nn.Sequential(
                ConvNorm(512, num_mel,
                         kernel_size=5, stride=1,
                         padding=2,
                         dilation=1, w_init_gain='linear'),
                nn.BatchNorm1d(num_mel))
            )

    def forward(self, x):
        for i in range(len(self.convolutions) - 1):
            x = torch.tanh(self.convolutions[i](x))

        x = self.convolutions[-1](x)

        return x


# Defines the GAN loss which uses either LSGAN or the regular GAN.
# When LSGAN is used, it is basically same as MSELoss,
# but it abstracts away the need to create the target label tensor
# that has the same size as the input

def pad_layer(inp, layer, is_2d=False):
    if type(layer.kernel_size) == tuple:
        kernel_size = layer.kernel_size[0]
    else:
        kernel_size = layer.kernel_size
    if not is_2d:
        if kernel_size % 2 == 0:
            pad = (kernel_size//2, kernel_size//2 - 1)
        else:
            pad = (kernel_size//2, kernel_size//2)
    else:
        if kernel_size % 2 == 0:
            pad = (kernel_size//2, kernel_size//2 - 1, kernel_size//2, kernel_size//2 - 1)
        else:
            pad = (kernel_size//2, kernel_size//2, kernel_size//2, kernel_size//2)
    # padding
    inp = F.pad(inp,
            pad=pad,
            mode='reflect')
    out = layer(inp)
    return out


class ConvLayer(nn.Module):
    
    def __init__(self, in_channels=1, out_channels=256):
        '''Constructs the ConvLayer with a specified input and output size.
           param in_channels: input depth of an image, default value = 1
           param out_channels: output depth of the convolutional layer, default value = 256
           '''
        super(ConvLayer, self).__init__()

        # defining a convolutional layer of the specified size
        self.conv = nn.Conv2d(in_channels, out_channels, 
                              kernel_size=9, stride=1, padding=0)

    def forward(self, x):
        '''Defines the feedforward behavior.
           param x: the input to the layer; an input image
           return: a relu-activated, convolutional layer
           '''
        # applying a ReLu activation to the outputs of the conv layer
        features = F.relu(self.conv(x)) # will have dimensions (batch_size, 20, 20, 256)
        return features

class PrimaryCaps(nn.Module):
    
    def __init__(self, num_capsules=8, in_channels=256, out_channels=32):
        '''Constructs a list of convolutional layers to be used in 
           creating capsule output vectors.
           param num_capsules: number of capsules to create
           param in_channels: input depth of features, default value = 256
           param out_channels: output depth of the convolutional layers, default value = 32
           '''
        super(PrimaryCaps, self).__init__()

        # creating a list of convolutional layers for each capsule I want to create
        # all capsules have a conv layer with the same parameters
        self.capsules = nn.ModuleList([
            nn.Conv2d(in_channels=in_channels, out_channels=out_channels, 
                      kernel_size=9, stride=2, padding=0)
            for _ in range(num_capsules)])
    
    def forward(self, x):
        '''Defines the feedforward behavior.
           param x: the input; features from a convolutional layer
           return: a set of normalized, capsule output vectors
           '''
        # get batch size of inputs
        batch_size = x.size(0)
        # reshape convolutional layer outputs to be (batch_size, vector_dim=1152, 1)
        u = [capsule(x).view(batch_size, 32 * 6 * 6, 1) for capsule in self.capsules]
        # stack up output vectors, u, one for each capsule
        u = torch.cat(u, dim=-1)
        # squashing the stack of vectors
        u_squash = self.squash(u)
        return u_squash
    def squash(self, input_tensor):
        '''Squashes an input Tensor so it has a magnitude between 0-1.
           param input_tensor: a stack of capsule inputs, s_j
           return: a stack of normalized, capsule output vectors, v_j
           '''
        squared_norm = (input_tensor ** 2).sum(dim=-1, keepdim=True)
        scale = squared_norm / (1 + squared_norm) # normalization coeff
        output_tensor = scale * input_tensor / torch.sqrt(squared_norm)    
        return output_tensor    

# to get transpose softmax function

# dynamic routing
def dynamic_routing(b_ij, u_hat, squash, routing_iterations=3):
    '''Performs dynamic routing between two capsule layers.
       param b_ij: initial log probabilities that capsule i should be coupled to capsule j
       param u_hat: input, weighted capsule vectors, W u
       param squash: given, normalizing squash function
       param routing_iterations: number of times to update coupling coefficients
       return: v_j, output capsule vectors
       '''    
    # update b_ij, c_ij for number of routing iterations
    for iteration in range(routing_iterations):
        # softmax calculation of coupling coefficients, c_ij
        c_ij = softmax(b_ij, dim=2)

        # calculating total capsule inputs, s_j = sum(c_ij*u_hat)
        s_j = (c_ij * u_hat).sum(dim=2, keepdim=True)

        # squashing to get a normalized vector output, v_j
        v_j = squash(s_j)

        # if not on the last iteration, calculate agreement and new b_ij
        if iteration < routing_iterations - 1:
            # agreement
            a_ij = (u_hat * v_j).sum(dim=-1, keepdim=True)
            
            # new b_ij
            b_ij = b_ij + a_ij
    
    return v_j # return latest v_j

class DigitCaps(nn.Module):
    
    def __init__(self, num_capsules=10, previous_layer_nodes=32*6*6, 
                 in_channels=8, out_channels=16):
        '''Constructs an initial weight matrix, W, and sets class variables.
           param num_capsules: number of capsules to create
           param previous_layer_nodes: dimension of input capsule vector, default value = 1152
           param in_channels: number of capsules in previous layer, default value = 8
           param out_channels: dimensions of output capsule vector, default value = 16
           '''
        super(DigitCaps, self).__init__()

        # setting class variables
        self.num_capsules = num_capsules
        self.previous_layer_nodes = previous_layer_nodes # vector input (dim=1152)
        self.in_channels = in_channels # previous layer's number of capsules

        # starting out with a randomly initialized weight matrix, W
        # these will be the weights connecting the PrimaryCaps and DigitCaps layers
        self.W = nn.Parameter(torch.randn(num_capsules, previous_layer_nodes, 
                                          in_channels, out_channels))

    def forward(self, u):
        '''Defines the feedforward behavior.
           param u: the input; vectors from the previous PrimaryCaps layer
           return: a set of normalized, capsule output vectors
           '''
        
        # adding batch_size dims and stacking all u vectors
        u = u[None, :, :, None, :]
        # 4D weight matrix
        W = self.W[:, None, :, :, :]
        
        # calculating u_hat = W*u
        u_hat = torch.matmul(u, W)
        # getting the correct size of b_ij
        # setting them all to 0, initially
        b_ij = torch.zeros(*u_hat.size())
        
        # moving b_ij to GPU, if available
        if TRAIN_ON_GPU:
            b_ij = b_ij.cuda()

        # update coupling coefficients and calculate v_j
        v_j = dynamic_routing(b_ij, u_hat, self.squash, routing_iterations=3)

        return v_j # return final vector outputs

    def squash(self, input_tensor):
        '''Squashes an input Tensor so it has a magnitude between 0-1.
           param input_tensor: a stack of capsule inputs, s_j
           return: a stack of normalized, capsule output vectors, v_j
           '''
        # same squash function as before
        squared_norm = (input_tensor ** 2).sum(dim=-1, keepdim=True)
        scale = squared_norm / (1 + squared_norm) # normalization coeff
        output_tensor = scale * input_tensor / torch.sqrt(squared_norm)    
        return output_tensor

class Decodar(nn.Module):
    
    def __init__(self, input_vector_length=16, input_capsules=10, hidden_dim=512):
        '''Constructs an series of linear layers + activations.
           param input_vector_length: dimension of input capsule vector, default value = 16
           param input_capsules: number of capsules in previous layer, default value = 10
           param hidden_dim: dimensions of hidden layers, default value = 512
           '''
        super(Decodar, self).__init__()
        
        # calculate input_dim
        input_dim = input_vector_length * input_capsules
        
        # define linear layers + activations
        self.linear_layers = nn.Sequential(
            nn.Linear(input_dim, hidden_dim), # first hidden layer
            nn.ReLU(inplace=True),
            nn.Linear(hidden_dim, hidden_dim*2), # second, twice as deep
            nn.ReLU(inplace=True),
            nn.Linear(hidden_dim*2, 28*28), # can be reshaped into 28*28 image
            nn.Sigmoid() # sigmoid activation to get output pixel values in a range from 0-1
            )
        
    def forward(self, x):
        '''Defines the feedforward behavior.
           param x: the input; vectors from the previous DigitCaps layer
           return: two things, reconstructed images and the class scores, y
           '''
        classes = (x ** 2).sum(dim=-1) ** 0.5
        classes = F.softmax(classes, dim=-1)
        
        # find the capsule with the maximum vector length
        # here, vector length indicates the probability of a class' existence
        _, max_length_indices = classes.max(dim=1)
        
        # create a sparse class matrix
        sparse_matrix = torch.eye(10) # 10 is the number of classes
        if TRAIN_ON_GPU:
            sparse_matrix = sparse_matrix.cuda()
        # get the class scores from the "correct" capsule
        y = sparse_matrix.index_select(dim=0, index=max_length_indices.data)
        
        # create reconstructed pixels
        x = x * y[:, :, None]
        # flatten image into a vector shape (batch_size, vector_dim)
        flattened_x = x.contiguous().view(x.size(0), -1)
        # create reconstructed image vectors
        reconstructions = self.linear_layers(flattened_x)
        
        # return reconstructions and the class scores, y
        return reconstructions, y


class PatchDiscriminator(nn.Module):
    
    def __init__(self):
        '''Constructs a complete Capsule Network.'''
        super(CapsuleNetwork, self).__init__()
        self.conv_layer = ConvLayer()
        self.primary_capsules = PrimaryCaps()
        self.digit_capsules = DigitCaps()
        self.decodar = Decodar()
                
    def forward(self, images):
        '''Defines the feedforward behavior.
           param images: the original MNIST image input data
           return: output of DigitCaps layer, reconstructed images, class scores
           '''
        primary_caps_output = self.primary_capsules(self.conv_layer(images))
        caps_output = self.digit_capsules(primary_caps_output).squeeze().transpose(0,1)
        reconstructions, y = self.decodar(caps_output)
        return caps_output, reconstructions, y


class Generator(nn.Module):
    """Generator network."""
    def __init__(self, dim_neck,  dim_pre, freq, dim_spec=80, is_train=False, lr=0.001, loss_content=True,
                 discriminator=False, multigpu=False, lambda_gan=0.0001,
                 lambda_wavenet=0.001, args=None,
                 test_path=None):
        super(Generator, self).__init__()

        self.encoder = MyEncoder(dim_neck, freq, num_mel=dim_spec)
        self.decoder1 = Decoder(dim_neck, 0, dim_pre, num_mel=dim_spec)

        self.decoder2 = Decoder(dim_neck, 0, dim_pre, num_mel=dim_spec)
        self.postnet1 = Postnet(num_mel=dim_spec)
        self.postnet2 = Postnet(num_mel=dim_spec)

	#if discriminator:
            #self.dis = PatchDiscriminator(n_class=num_speakers)
            #self.dis_criterion = GANLoss(use_lsgan=use_lsgan, tensor=torch.cuda.FloatTensor)
        #else:
            #self.dis = None

        self.loss_content = loss_content
        self.lambda_gan = lambda_gan
        self.lambda_wavenet = lambda_wavenet

        self.multigpu = multigpu
        if test_path is not None:
            self.prepare_test(dim_spec, test_path)

        self.vocoder = WaveRNN(
            rnn_dims=hparams.voc_rnn_dims,
            fc_dims=hparams.voc_fc_dims,
            bits=hparams.bits,
            pad=hparams.voc_pad,
            upsample_factors=hparams.voc_upsample_factors,
            feat_dims=hparams.num_mels,
            compute_dims=hparams.voc_compute_dims,
            res_out_dims=hparams.voc_res_out_dims,
            res_blocks=hparams.voc_res_blocks,
            hop_length=hparams.hop_size,
            sample_rate=hparams.sample_rate,
            mode=hparams.voc_mode
        )
        
        if is_train:
            self.criterionIdt = torch.nn.L1Loss(reduction='mean')
            self.contentIdt = torch.nn.MSELoss()
            self.opt_encoder = torch.optim.Adam(self.encoder.parameters(), lr=lr)
            self.opt_decoder1 = torch.optim.Adam(itertools.chain(self.decoder1.parameters(), self.postnet1.parameters()), lr=lr)
            self.opt_decoder2 = torch.optim.Adam(itertools.chain(self.decoder2.parameters(), self.postnet2.parameters()), lr=lr)
            self.opt_vocoder = torch.optim.Adam(self.vocoder.parameters(), lr=hparams.voc_lr)
            self.vocoder_loss_func = F.cross_entropy # Only for RAW


        if multigpu:
            self.encoder = nn.DataParallel(self.encoder)
            self.decoder1 = nn.DataParallel(self.decoder1)
            self.postnet1 = nn.DataParallel(self.postnet1)
            self.decoder2 = nn.DataParallel(self.decoder2)
            self.postnet2 = nn.DataParallel(self.postnet2)
            self.vocoder = nn.DataParallel(self.vocoder)


    def prepare_test(self, dim_spec, test_path):
        mel_basis80 = librosa.filters.mel(hparams.sample_rate, hparams.n_fft, n_mels=80)

        wav, sr = librosa.load(test_path, hparams.sample_rate)
        wav = preemphasis(wav, hparams.preemphasis, hparams.preemphasize)
        linear_spec = np.abs(
            librosa.stft(wav, n_fft=hparams.n_fft, hop_length=hparams.hop_size, win_length=hparams.win_size))
        mel_spec = mel_basis80.dot(linear_spec)
        mel_db = 20 * np.log10(mel_spec)
        source_spec = np.clip((mel_db + 120) / 125, 0, 1)

        self.test_wav = wav

        self.test_spec = torch.Tensor(pad_seq(source_spec.T, hparams.freq)).unsqueeze(0)

    def test_fixed(self, device):
        with torch.no_grad():
            s2t_spec = self.conversion(self.test_spec, device).cpu()

        ret_dic = {}
        ret_dic['A_fake_griffin'], sr = mel2wav(s2t_spec.numpy().squeeze(0).T)
        ret_dic['A'] = self.test_wav

        with torch.no_grad():
            if not self.multigpu:
                ret_dic['A_fake_w'] = inv_preemphasis(self.vocoder.generate(s2t_spec.to(device).transpose(2, 1), False, None, None, mu_law=True),
                                                hparams.preemphasis, hparams.preemphasize)
            else:
                ret_dic['A_fake_w'] = inv_preemphasis(self.vocoder.module.generate(s2t_spec.to(device).transpose(2, 1), False, None, None, mu_law=True),
                                                hparams.preemphasis, hparams.preemphasize)
        return ret_dic, sr


    def conversion(self, spec, device, speed=1):
        spec = spec.to(device)
        if not self.multigpu:
            codes = self.encoder(spec)
        else:
            codes = self.encoder.module(spec)
        tmp = []
        for code in codes:
            tmp.append(code.unsqueeze(1).expand(-1, int(speed * spec.size(1) / len(codes)), -1))
        code_exp = torch.cat(tmp, dim=1)
        mel_outputs = self.decoder1(code_exp) if not self.multigpu else self.decoder1.module(code_exp)

        mel_outputs_postnet = self.postnet1(mel_outputs.transpose(2, 1))
        mel_outputs_postnet = mel_outputs + mel_outputs_postnet.transpose(2, 1)
        return mel_outputs_postnet

    def optimize_parameters(self, dataloader1,dataloader2, epochs, device, display_freq=10, save_freq=1000, save_dir="./",
                            experimentName="Train", load_model=None, initial_niter=0):
        writer = SummaryWriter(log_dir="logs/"+experimentName)
        if load_model is not None:
            print("Loading from %s..." % load_model)

            d = torch.load(load_model)
            newdict = d.copy()
            for key, value in d.items():
                newkey = key
                if 'wavenet' in key:
                    newdict[key.replace('wavenet', 'vocoder')] = newdict.pop(key)
                    newkey = key.replace('wavenet', 'vocoder')
                if self.multigpu and 'module' not in key:
                    newdict[newkey.replace('.','.module.',1)] = newdict.pop(newkey)
                    newkey = newkey.replace('.', '.module.', 1)
                if newkey not in self.state_dict():
                    newdict.pop(newkey)
            self.load_state_dict(newdict)
            print("AutoVC Model Loaded")
        niter = initial_niter
        for epoch in range(epochs):
            self.train()
            for i, data in enumerate(zip(cycle(dataloader1),dataloader2)):
                speaker_org1, spec1, prev1, wav1 = data[0]
                speaker_org2, spec2, prev2, wav2 = data[1]
                loss_dict, loss_dict_discriminator, loss_dict_wavenet = \
                    self.train_step(spec1.to(device),  spec2.to(device), prev1=prev1.to(device), wav1=wav1.to(device),
                                     prev2=prev2.to(device),
                                    wav2=wav2.to(device),
                                    device=device)
                if niter % display_freq == 0:
                    print("Epoch[%d] Iter[%d] Niter[%d] %s %s %s"
                          % (epoch, i, niter, loss_dict, loss_dict_discriminator, loss_dict_wavenet))
                    writer.add_scalars('data/Loss', loss_dict,
                                       niter)
                    if loss_dict_discriminator != {}:
                        writer.add_scalars('data/discriminator', loss_dict_discriminator, niter)
                    if loss_dict_wavenet != {}:
                        writer.add_scalars('data/wavenet', loss_dict_wavenet, niter)
                if niter % save_freq == 0:
                    print("Saving and Testing...", end='\t')
                    torch.save(self.state_dict(), save_dir + '/Epoch' + str(epoch).zfill(3) + '_Iter'
                               + str(niter).zfill(8) + ".pkl")

                    if len(dataloader1) >= 2 and self.test_wav is not None:
                        wav_dic, sr = self.test_fixed(device)
                        for key, wav in wav_dic.items():

                            writer.add_audio(key, wav, niter, sample_rate=sr)
                        librosa.output.write_wav(save_dir + '/Iter' + str(niter).zfill(8) +'.wav', wav_dic['A_fake_w'].astype(np.float32), hparams.sample_rate)
                    print("Done")
                    self.train()
                torch.cuda.empty_cache()  # Prevent Out of Memory
                niter += 1


    def train_step(self, x1, x2, prev1=None, wav1=None,
                   prev2=None,wav2=None,ret_content=False, retain_graph=False, device='cuda:0'):
        #spk1+cycle1
        codes1 = self.encoder(x1)

        tmp1 = []
        for code in codes1:
            tmp1.append(code.unsqueeze(1).expand(-1, int(x1.size(1) / len(codes1)), -1))
        code_exp1 = torch.cat(tmp1, dim=1)

        
        #spk2+cycle1
        codes2 = self.encoder(x2)
        content2 = torch.cat([code.unsqueeze(1) for code in codes2], dim=1)
        tmp2 = []
        for code in codes2:
            tmp2.append(code.unsqueeze(1).expand(-1, int(x2.size(1) / len(codes2)), -1))
        code_exp2 = torch.cat(tmp2, dim=1)

        # spk1+encoder1+decoder1
        mel_outputs1 = self.decoder1(code_exp1)
        mel_outputs_postnet1 = self.postnet1(mel_outputs1.transpose(2, 1))
        mel_outputs_postnet1 = mel_outputs1 + mel_outputs_postnet1.transpose(2, 1)
        
        # spk2+encoder1+decoder2
        mel_outputs2 = self.decoder2(code_exp2)
        mel_outputs_postnet2 = self.postnet2(mel_outputs2.transpose(2, 1))
        mel_outputs_postnet2 = mel_outputs2 + mel_outputs_postnet2.transpose(2, 1)
        
        #spk1+encoder1+decoder2
        mel_outputs1_2 = self.decoder2(code_exp1)
        mel_outputs_postnet1_2 = self.postnet2(mel_outputs1_2.transpose(2, 1))
        mel_outputs_postnet1_2 = mel_outputs1_2 + mel_outputs_postnet1_2.transpose(2, 1)

        #spk2+encoder1+decoder1
        mel_outputs2_1 = self.decoder1(code_exp2)
        mel_outputs_postnet2_1 = self.postnet1(mel_outputs2_1.transpose(2, 1))
        mel_outputs_postnet2_1 = mel_outputs2_1 + mel_outputs_postnet2_1.transpose(2, 1)
         
        #spk1+cycle2
        codes1_2 = self.encoder(mel_outputs_postnet1_2)

        tmp1_2 = []
        for code in codes1_2:
            tmp1_2.append(code.unsqueeze(1).expand(-1, int(x1.size(1) / len(codes1_2)), -1))
        code_exp1_2 = torch.cat(tmp1_2, dim=1)
        mel_outputs1new = self.decoder1(code_exp1_2)
        mel_outputs_postnet1new = self.postnet1(mel_outputs1new.transpose(2, 1))
        mel_outputs_postnet1new = mel_outputs1new + mel_outputs_postnet1new.transpose(2, 1)
        
        #spk2+cycle2
        codes2_1 = self.encoder(mel_outputs_postnet2_1)

        tmp2_1 = []
        for code in codes2_1:
            tmp2_1.append(code.unsqueeze(1).expand(-1, int(x2.size(1) / len(codes2_1)), -1))
        code_exp2_1 = torch.cat(tmp2_1, dim=1)
        mel_outputs2new = self.decoder2(code_exp2_1)
        mel_outputs_postnet2new = self.postnet2(mel_outputs2new.transpose(2, 1))
        mel_outputs_postnet2new = mel_outputs2new + mel_outputs_postnet2new.transpose(2, 1)
        
        loss_dict, loss_dict_discriminator, loss_dict_wavenet = {}, {}, {}

        loss_recon = self.criterionIdt(x1, mel_outputs1)+self.criterionIdt(x2,mel_outputs2)
        loss_recon0 = self.criterionIdt(x1, mel_outputs_postnet1)+self.criterionIdt(x2,mel_outputs_postnet2)
        loss_dict['recon'], loss_dict['recon0'] = loss_recon.data.item(), loss_recon0.data.item()
        if self.loss_content:

            loss_cycle1 = self.contentIdt(mel_outputs_postnet1new,mel_outputs_postnet1)
            loss_cycle2 = self.contentIdt(mel_outputs_postnet2new,mel_outputs_postnet2)

            loss_content = loss_cycle1+loss_cycle2
            loss_dict['content'] = loss_content.data.item()
        else:
            loss_content = torch.from_numpy(np.array(0))

        loss_gen, loss_dis, loss_vocoder = [torch.from_numpy(np.array(0))] * 3



        if not self.multigpu:
            y_hat = self.vocoder(prev1,
                                self.vocoder.pad_tensor(mel_outputs_postnet1, hparams.voc_pad).transpose(1, 2))
        else:
            y_hat = self.vocoder(prev1,self.vocoder.module.pad_tensor(mel_outputs_postnet1, hparams.voc_pad).transpose(1, 2))
        y_hat = y_hat.transpose(1, 2).unsqueeze(-1)
        # assert (0 <= wav < 2 ** 9).all()
        loss_vocoder = self.vocoder_loss_func(y_hat, wav1.unsqueeze(-1).to(device))
        self.opt_vocoder.zero_grad()

        Loss = loss_recon + loss_recon0 + loss_content+ self.lambda_gan * loss_gen + self.lambda_wavenet * loss_vocoder
        loss_dict['total'] = Loss.data.item()
        self.opt_encoder.zero_grad()
        self.opt_decoder1.zero_grad()
        self.opt_decoder2.zero_grad()
        Loss.backward(retain_graph=retain_graph)
        self.opt_encoder.step()
        self.opt_decoder1.step()
        self.opt_decoder2.step()

        self.opt_vocoder.step()

        if ret_content:
            return loss_recon, loss_recon0, loss_content, Loss
        return loss_dict, loss_dict_discriminator, loss_dict_wavenet

class VideoAudioGenerator(nn.Module):
    def __init__(self, dim_neck, dim_emb, dim_pre, freq, dim_spec=80, is_train=False, lr=0.001,
                 multigpu=False, 
                 lambda_wavenet=0.001, args=None,
                 residual=False, attention_map=None, use_256=False, loss_content=False,
                 test_path=None):
        super(VideoAudioGenerator, self).__init__()

        self.encoder = MyEncoder(dim_neck, freq, num_mel=dim_spec)

        self.decoder = Decoder(dim_neck, 0, dim_pre, num_mel=dim_spec)
        self.postnet = Postnet(num_mel=dim_spec)
        if use_256:
            self.video_decoder = VideoGenerator(use_256=True)
        else:
            self.video_decoder = STAGE2_G(residual=residual)
        self.use_256 = use_256
        self.lambda_wavenet = lambda_wavenet
        self.loss_content = loss_content
        self.multigpu = multigpu
        self.test_path = test_path

        self.vocoder = WaveRNN(
            rnn_dims=hparams.voc_rnn_dims,
            fc_dims=hparams.voc_fc_dims,
            bits=hparams.bits,
            pad=hparams.voc_pad,
            upsample_factors=hparams.voc_upsample_factors,
            feat_dims=hparams.num_mels,
            compute_dims=hparams.voc_compute_dims,
            res_out_dims=hparams.voc_res_out_dims,
            res_blocks=hparams.voc_res_blocks,
            hop_length=hparams.hop_size,
            sample_rate=hparams.sample_rate,
            mode=hparams.voc_mode
        )

        if is_train:
            self.criterionIdt = torch.nn.L1Loss(reduction='mean')
            self.opt_encoder = torch.optim.Adam(self.encoder.parameters(), lr=lr)
            self.opt_decoder = torch.optim.Adam(itertools.chain(self.decoder.parameters(), self.postnet.parameters()), lr=lr)
            self.opt_video_decoder = torch.optim.Adam(self.video_decoder.parameters(), lr=lr)

            self.opt_vocoder = torch.optim.Adam(self.vocoder.parameters(), lr=hparams.voc_lr)
            self.vocoder_loss_func = F.cross_entropy # Only for RAW

        if multigpu:
            self.encoder = nn.DataParallel(self.encoder)
            self.decoder = nn.DataParallel(self.decoder)
            self.video_decoder = nn.DataParallel(self.video_decoder)
            self.postnet = nn.DataParallel(self.postnet)
            self.vocoder = nn.DataParallel(self.vocoder)
    
    def optimize_parameters_video(self, dataloader, epochs, device, display_freq=10, save_freq=1000, save_dir="./",
                            experimentName="Train", initial_niter=0, load_model=None):
        writer = SummaryWriter(log_dir="logs/" + experimentName)
        if load_model is not None:
            print("Loading from %s..." % load_model)
            # self.load_state_dict(torch.load(load_model))
            d = torch.load(load_model)
            newdict = d.copy()
            for key, value in d.items():
                newkey = key
                if 'wavenet' in key:
                    newdict[key.replace('wavenet', 'vocoder')] = newdict.pop(key)
                    newkey = key.replace('wavenet', 'vocoder')
                if self.multigpu and 'module' not in key:
                    newdict[newkey.replace('.','.module.',1)] = newdict.pop(newkey)
                    newkey = newkey.replace('.', '.module.', 1)
                if newkey not in self.state_dict():
                    newdict.pop(newkey)
            print("Load " + str(len(newdict)) + " parameters!")
            self.load_state_dict(newdict, strict=False)
            print("AutoVC Model Loaded") 
        niter = initial_niter
        for epoch in range(epochs):
            self.train()
            for i, data in enumerate(dataloader):
                # print("Processing ..." + str(name))
                speaker, mel, prev, wav, video, video_large = data
                speaker, mel, prev, wav, video, video_large = speaker.to(device), mel.to(device), prev.to(device), wav.to(device), video.to(device), video_large.to(device)
                codes, code_unsample = self.encoder(mel, return_unsample=True)
                
                tmp = []
                for code in codes:
                    tmp.append(code.unsqueeze(1).expand(-1, int(mel.size(1) / len(codes)), -1))
                code_exp = torch.cat(tmp, dim=1)

                if not self.use_256:
                    v_stage1, v_stage2 = self.video_decoder(code_unsample, train=True)
                else:
                    v_stage2 = self.video_decoder(code_unsample)
                mel_outputs = self.decoder(code_exp)
                mel_outputs_postnet = self.postnet(mel_outputs.transpose(2, 1))
                mel_outputs_postnet = mel_outputs + mel_outputs_postnet.transpose(2, 1)

                if self.loss_content:
                    _, recons_codes = self.encoder(mel_outputs_postnet, speaker, return_unsample=True)
                    loss_content = self.criterionIdt(code_unsample, recons_codes)
                else:
                    loss_content = torch.from_numpy(np.array(0))
                
                if not self.use_256:
                    loss_video = self.criterionIdt(v_stage1, video) + self.criterionIdt(v_stage2, video_large)
                else:
                    loss_video = self.criterionIdt(v_stage2, video_large)
                
                loss_recon = self.criterionIdt(mel, mel_outputs)
                loss_recon0 = self.criterionIdt(mel, mel_outputs_postnet)
                loss_vocoder = 0

                if not self.multigpu:
                    y_hat = self.vocoder(prev,
                                    self.vocoder.pad_tensor(mel_outputs_postnet, hparams.voc_pad).transpose(1, 2))
                else:
                    y_hat = self.vocoder(prev,self.vocoder.module.pad_tensor(mel_outputs_postnet, hparams.voc_pad).transpose(1, 2))
                y_hat = y_hat.transpose(1, 2).unsqueeze(-1)
                # assert (0 <= wav < 2 ** 9).all()
                loss_vocoder = self.vocoder_loss_func(y_hat, wav.unsqueeze(-1).to(device))
                self.opt_vocoder.zero_grad()

                loss = loss_video + loss_recon + loss_recon0 + self.lambda_wavenet * loss_vocoder + loss_content

                self.opt_encoder.zero_grad()
                self.opt_decoder.zero_grad()
                self.opt_video_decoder.zero_grad()
                loss.backward()
                self.opt_encoder.step()
                self.opt_decoder.step()
                self.opt_video_decoder.step()
                self.opt_vocoder.step()



                if niter % display_freq == 0:
                    print("Epoch[%d] Iter[%d] Niter[%d] %s"
                          % (epoch, i, niter, loss.data.item()))
                    writer.add_scalars('data/Loss', {'loss':loss.data.item(),
                                                    'loss_video':loss_video.data.item(),
                                                    'loss_audio':loss_recon0.data.item()+loss_recon.data.item()}, niter)

                if niter % save_freq == 0:
                    torch.cuda.empty_cache()  # Prevent Out of Memory
                    print("Saving and Testing...", end='\t')
                    torch.save(self.state_dict(), save_dir + '/Epoch' + str(epoch).zfill(3) + '_Iter'
                               + str(niter).zfill(8) + ".pkl")
                    # self.load_state_dict(torch.load('params.pkl'))
                    self.test_audiovideo(device, writer, niter)
                    print("Done")
                    self.train()
                torch.cuda.empty_cache()  # Prevent Out of Memory
                niter += 1

    def generate(self, mel, speaker, device='cuda:0'):
        mel, speaker = mel.to(device), speaker.to(device)
        if not self.multigpu:
            codes, code_unsample = self.encoder(mel, return_unsample=True)
        else:
            codes, code_unsample = self.encoder.module(mel, speaker, return_unsample=True)
                
        tmp = []
        for code in codes:
            tmp.append(code.unsqueeze(1).expand(-1, int(mel.size(1) / len(codes)), -1))
        code_exp = torch.cat(tmp, dim=1)

        if not self.multigpu:
            if not self.use_256:
                v_stage1, v_stage2 = self.video_decoder(code_unsample, train=True)
            else:
                v_stage2 = self.video_decoder(code_unsample)
                v_stage1 = v_stage2
            mel_outputs = self.decoder(code_exp)
            mel_outputs_postnet = self.postnet(mel_outputs.transpose(2, 1))
        else:
            if not self.use_256:
                v_stage1, v_stage2 = self.video_decoder.module(code_unsample, train=True)
            else:
                v_stage2 = self.video_decoder.module(code_unsample)
                v_stage1 = v_stage2
            mel_outputs = self.decoder.module(code_exp)
            mel_outputs_postnet = self.postnet.module(mel_outputs.transpose(2, 1))
        
        mel_outputs_postnet = mel_outputs + mel_outputs_postnet.transpose(2, 1)
        
        return mel_outputs_postnet, v_stage1, v_stage2
    
    def test_video(self, device):
        wav, sr = librosa.load("/mnt/lustre/dengkangle/cmu/datasets/video/obama_test.mp4", hparams.sample_rate)
        mel_basis = librosa.filters.mel(hparams.sample_rate, hparams.n_fft, n_mels=hparams.num_mels)
        linear_spec = np.abs(
            librosa.stft(wav, n_fft=hparams.n_fft, hop_length=hparams.hop_size, win_length=hparams.win_size))
        mel_spec = mel_basis.dot(linear_spec)
        mel_db = 20 * np.log10(mel_spec)

        test_data = np.clip((mel_db + 120) / 125, 0, 1)
        test_data = torch.Tensor(pad_seq(test_data.T, hparams.freq)).unsqueeze(0).to(device)
        with torch.no_grad():
            codes, code_exp = self.encoder.module(test_data, return_unsample=True)
            v_mid, v_hat = self.video_decoder.module(code_exp, train=True)

        reader = imageio.get_reader("/mnt/lustre/dengkangle/cmu/datasets/video/obama_test.mp4", 'ffmpeg', fps=20)
        frames = []
        for i, im in enumerate(reader):
            frames.append(np.array(im).transpose(2, 0, 1))
        frames = (np.array(frames) / 255 - 0.5) / 0.5
        return frames, v_mid[0:1], v_hat[0:1]

    def test_audiovideo(self, device, writer, niter):
        source_path = self.test_path

        mel_basis80 = librosa.filters.mel(hparams.sample_rate, hparams.n_fft, n_mels=80)

        wav, sr = librosa.load(source_path, hparams.sample_rate)
        wav = preemphasis(wav, hparams.preemphasis, hparams.preemphasize)

        linear_spec = np.abs(
            librosa.stft(wav, n_fft=hparams.n_fft, hop_length=hparams.hop_size, win_length=hparams.win_size))
        mel_spec = mel_basis80.dot(linear_spec)
        mel_db = 20 * np.log10(mel_spec)
        source_spec = np.clip((mel_db + 120) / 125, 0, 1)
        
        source_embed = torch.from_numpy(np.array([0, 1])).float().unsqueeze(0)
        source_wav = wav

        source_spec = torch.Tensor(pad_seq(source_spec.T, hparams.freq)).unsqueeze(0)
        # print(source_spec.shape)
        
        with torch.no_grad():
            generated_spec, v_mid, v_hat = self.generate(source_spec, source_embed ,device)

        generated_spec, v_mid, v_hat = generated_spec.cpu(), v_mid.cpu(), v_hat.cpu()

        print("Generating Wavfile...")
        with torch.no_grad():
            if not self.multigpu:
                generated_wav = inv_preemphasis(self.vocoder.generate(generated_spec.to(device).transpose(2, 1), False, None, None, mu_law=True), hparams.preemphasis, hparams.preemphasize)
            
            else:
                generated_wav = inv_preemphasis(self.vocoder.module.generate(generated_spec.to(device).transpose(2, 1), False, None, None, mu_law=True), hparams.preemphasis, hparams.preemphasize)


        writer.add_video('generated', (v_hat.numpy()+1)/2, global_step=niter)
        writer.add_video('mid', (v_mid.numpy()+1)/2, global_step=niter)
        writer.add_audio('ground_truth', source_wav, niter, sample_rate=hparams.sample_rate)
        writer.add_audio('generated_wav', generated_wav, niter, sample_rate=hparams.sample_rate)
   
