import numpy as np
import caffe
import cudarray as ca
import deeppy as dp
from deeppy.base import Model
from deeppy.parameter import Parameter


def gram_matrix(img_bc01):
    n_channels = img_bc01.shape[1]
    #change reshape to numpy
    feats = np.reshape(img_bc01, (n_channels, -1))
    #feats = ca.reshape(img_bc01, (n_channels, -1))
    featsT = feats.T
    gram = np.dot(feats, featsT)
    return gram

def weight_array(weights):
    array = np.zeros(19)
    for idx, weight in weights:
        array[idx] = weight
    norm = np.sum(array)
    if norm > 0:
        array /= norm
    return array


class StyleNet(caffe.Net):

    def __init__(self, prototxt, params_file, subject_img, style_img, subject_weights, style_weights, subject_ratio,
                 layers = None, init_img = None, mean = None, channel_swap = None, smoothness=0.0, init_noise = 0.0):

        caffe.Net.__init__(self, prototxt, params_file, caffe.TEST)

        # configure pre-processing
        in_ = self.inputs[0]
        self.transformer = caffe.io.Transformer(
            {in_: self.blobs[in_].data.shape})
        self.transformer.set_transpose(in_, (2, 0, 1))
        if mean is not None:
            self.transformer.set_mean(in_, mean)
        if channel_swap is not None:
            self.transformer.set_channel_swap(in_, channel_swap)

        self.crop_dims = np.array(self.blobs[in_].data.shape[2:])
        self.image_dims = self.crop_dims

        #net = caffe.Classifier(prototxt, params_file,
        #                   mean = mean,                 # ImageNet mean, training set dependent
        #                   channel_swap = channel_swap) # the reference model has channels in BGR order instead of RGB
        if layers is None:
            layers = self.layers;

        # Map weights (in convolution indices) to layer indices
        subject_weights = weight_array(subject_weights) * subject_ratio
        style_weights = weight_array(style_weights)
        self.subject_weights = np.zeros(len(layers))
        self.style_weights = np.zeros(len(layers))
        layers_len = 0
        conv_idx = 0
        for l, layer in enumerate(layers):
            if layer.type == "ReLU":
                if l < len(subject_weights):
                    self.subject_weights[l] = subject_weights[conv_idx]
                    self.style_weights[l] = style_weights[conv_idx]
                    if subject_weights[conv_idx] > 0 or \
                       style_weights[conv_idx] > 0:
                        layers_len = l+1
                    conv_idx += 1

        init_img = subject_img
        noise = np.random.normal(size=init_img.shape, scale=np.std(init_img)*1e-1)
        init_img = init_img * (1 - init_noise) + noise * init_noise

        """def output_shape(blob, x_shape):
            b, _, img_h, img_w = x_shape
            filter_shape = blob.shape[2:]
            out_shape = ((img_h + 2*padding[0] - filter_shape[0]) //
                         strides[0] + 1,
                         (img_w + 2*padding[1] - filter_shape[1]) //
                         strides[1] + 1)
            return (b, n_filters) + out_shape"""

        # Setup network
        x_shape = init_img.shape
        self.x = Parameter(init_img)
        self.x._setup(x_shape)
        for blob in self.blobs.values():
            shape = blob.shape
            blob.reshape(shape[0], shape[1], x_shape[0], x_shape[1])
            #x_shape = output_shape(blob, x_shape)
            #x_shape = (blob.shape

        # Precompute subject features and style Gram matrices
        self.subject_feats = [None]*len(self.layers)
        self.style_grams = [None]*len(self.layers)

        def preprocess(img):
            return np.float32(np.rollaxis(img, 2)[::-1]) - self.transformer.mean['data']

        def set_input(blob, octave, addNoise=False):
            detail = np.zeros_like(octave[-1]) # allocate image for network-produced details
            h, w = octave[0].shape[-2:]
            old_shape = blob.shape
            blob.reshape(1,blob.channels,h,w)
            if addNoise:
                blob.data[0] = octave[0]+detail
            else:
                blob.data[0] = octave[0]


        octaves_subj = [preprocess(subject_img)]
        octaves_style = [preprocess(style_img)]

        #next_subject = ca.array(subject_img)
        #next_style = ca.array(style_img)

        layers_name_list = self.blobs.keys()

        layer_idx = 0
        curr_blob_name = layers_name_list[layer_idx]
        curr_blob = self.blobs[curr_blob_name]
        next_blob_name = layers_name_list[layer_idx + 1]
        next_blob = self.blobs[next_blob_name]

        set_input(curr_blob, octaves_subj, True)
        next_subject = self.forward(end=next_blob_name)[next_blob_name]
        set_input(curr_blob, octaves_style, True)
        next_style = self.forward(end=next_blob_name)[next_blob_name]
        layer_idx += 2

        for l, layer in enumerate(self.layers):
            if layer_idx + 1 == len(layers_name_list):
                break
            curr_blob_name = layers_name_list[layer_idx]
            curr_blob = self.blobs[curr_blob_name]
            next_blob_name = layers_name_list[layer_idx + 1]
            next_blob = self.blobs[next_blob_name]

            #next_subject = layer.fprop(next_subject)
            #curr_blob.data[0] = next_subject
            set_input(curr_blob, next_subject)
            next_subject = self.forward(start=curr_blob_name, end=next_blob_name)[next_blob_name]

            #next_style = layer.fprop(next_style)
            #curr_blob.data[0] = next_style
            set_input(curr_blob, next_style)
            next_style = self.forward(start=curr_blob_name, end=next_blob_name)[next_blob_name]

            layer_idx += 1

            #iterate over list of layers\blobs
            if self.subject_weights[l] > 0:
                self.subject_feats[l] = next_subject
            if self.style_weights[l] > 0:
                gram = gram_matrix(next_style)
                # Scale gram matrix to compensate for different image sizes
                n_pixels_subject = np.prod(next_subject.shape[2:])
                n_pixels_style = np.prod(next_style.shape[2:])
                scale = (n_pixels_subject / float(n_pixels_style))
                self.style_grams[l] = gram * scale

        self.tv_weight = smoothness
        kernel = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=dp.float_)
        kernel /= np.sum(np.abs(kernel))
        self.tv_kernel = ca.array(kernel[np.newaxis, np.newaxis, ...])
        self.tv_conv = ca.nnet.ConvBC01((1, 1), (1, 1))

    @property
    def image(self):
        return np.array(self.x.array)

    @property
    def _params(self):
        return [self.x]

    def _update(self):

        # Forward propagation
        """next_x = self.x.array
        x_feats = [None]*len(self.layers)
        for l, layer in enumerate(self.layers):
            next_x = layer.fprop(next_x)
            if self.subject_weights[l] > 0 or self.style_weights[l] > 0:
                x_feats[l] = next_x

        # Backward propagation
        grad = ca.zeros_like(next_x)
        loss = ca.zeros(1)
        for l, layer in reversed(list(enumerate(self.layers))):
            if self.subject_weights[l] > 0:
                diff = x_feats[l] - self.subject_feats[l]
                norm = ca.sum(ca.fabs(diff)) + 1e-8
                weight = float(self.subject_weights[l]) / norm
                grad += diff * weight
                loss += 0.5*weight*ca.sum(diff**2)
            if self.style_weights[l] > 0:
                diff = gram_matrix(x_feats[l]) - self.style_grams[l]
                n_channels = diff.shape[0]
                x_feat = ca.reshape(x_feats[l], (n_channels, -1))
                style_grad = ca.reshape(ca.dot(diff, x_feat), x_feats[l].shape)
                norm = ca.sum(ca.fabs(style_grad))
                weight = float(self.style_weights[l]) / norm
                style_grad *= weight
                grad += style_grad
                loss += 0.25*weight*ca.sum(diff**2)
            grad = layer.bprop(grad)

        if self.tv_weight > 0:
            x = ca.reshape(self.x.array, (3, 1) + grad.shape[2:])
            tv = self.tv_conv.fprop(x, self.tv_kernel)
            tv *= self.tv_weight
            grad -= ca.reshape(tv, grad.shape)

        ca.copyto(self.x.grad_array, grad)
        return loss"""