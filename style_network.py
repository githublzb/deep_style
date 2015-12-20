import numpy as np
import cudarray as ca
import deeppy as dp
from deeppy.base import Model
from deeppy.parameter import Parameter
from caffe_style.debug_logger import Logger
from pickle import dump

class Convolution(dp.Convolution):
    """ Convolution layer wrapper

    This layer does not propagate gradients to filters. Also, it reduces
    memory consumption as it does not store fprop() input for bprop().
    """

    def __init__(self, layer):
        self.layer = layer

    def fprop(self, x):
        y = self.conv_op.fprop(x, self.weights.array)
        y += self.bias.array
        return y

    def bprop(self, y_grad):
        # Backprop to input image only
        _, x_grad = self.layer.conv_op.bprop(
            imgs=None, filters=self.weights.array, convout_d=y_grad,
            to_imgs=True, to_filters=False
        )
        return x_grad

    # Wrap layer methods
    def __getattr__(self, attr):
        if attr in self.__dict__:
            return getattr(self, attr)
        return getattr(self.layer, attr)

    def __str__(self):
        return self.name

    def __repr__(self):
        return self.name


def gram_matrix(img_bc01):
    n_channels = img_bc01.shape[1]
    feats = ca.reshape(img_bc01, (n_channels, -1))
    gram = ca.dot(feats, feats.T)
    return gram


class StyleNetwork(Model):
    """ Artistic style network

    Implementation of [1].

    Differences:
    - The gradients for both subject and style are normalized. The original
      method uses pre-normalized convolutional features.
    - The Gram matrices are scaled wrt. # of pixels. The original method is
      sensitive to different image sizes between subject and style.
    - Additional smoothing term for visually better results.

    References:
    [1]: A Neural Algorithm of Artistic Style; Leon A. Gatys, Alexander S.
         Ecker, Matthias Bethge; arXiv:1508.06576; 08/2015
    """

    def __init__(self, layers, init_img, subject_img, style_img,
                 subject_weights, style_weights, smoothness=0.0):

        self.logger = Logger("deeppy_style", True, 1)

        # Map weights (in convolution indices) to layer indices
        self.subject_weights = np.zeros(len(layers))
        self.style_weights = np.zeros(len(layers))
        layers_len = 0
        conv_idx = 0
        for l, layer in enumerate(layers):
            if isinstance(layer, dp.Activation):
                print l, layer.name
                self.subject_weights[l] = subject_weights[conv_idx]
                self.style_weights[l] = style_weights[conv_idx]
                if subject_weights[conv_idx] > 0 or \
                                style_weights[conv_idx] > 0:
                    layers_len = l + 1
                conv_idx += 1

        # Discard unused layers
        layers = layers[:layers_len]

        # Wrap convolution layers for better performance
        self.layers = [Convolution(l) if isinstance(l, dp.Convolution) else l
                       for l in layers]

        layers_names = ['conv1_1', 'relu1_1', 'conv1_2', 'relu1_2', 'pool1', 'conv2_1', 'relu2_1', 'conv2_2', 'relu2_2',
                        'pool2', 'conv3_1', 'relu3_1', 'conv3_2', 'relu3_2', 'conv3_3', 'relu3_3', 'conv3_4', 'relu3_4',
                        'pool3', 'conv4_1', 'relu4_1', 'conv4_2', 'relu4_2', 'conv4_3', 'relu4_3', 'conv4_4', 'relu4_4',
                        'pool4', 'conv5_1', 'relu5_1']

        self.layers_names_map = dict(map(lambda (i, l): (l, layers_names[i]), enumerate(self.layers)))

        # Setup network
        x_shape = init_img.shape
        self.x = Parameter(init_img)
        self.x._setup(x_shape)
        for layer in self.layers:
            layer._setup(x_shape)
            x_shape = layer.y_shape(x_shape)
            self.logger.debug("%s : %s" % (layer.name, x_shape))

        # Precompute subject features and style Gram matrices
        self.subject_feats = [None] * len(self.layers)
        self.style_grams = [None] * len(self.layers)
        next_subject = ca.array(subject_img)
        # print "next_subject = %s" % next_subject[0][0][0][0]
        next_style = ca.array(style_img)
        self.logger.trace("next_subject[%-10s]: %s" % ('data', str(next_subject)[:60]))
        self.logger.debug("next_style  [%-10s]: %s" % ('data', str(next_style)[:60]))
        for l, layer in enumerate(self.layers):
            self.logger.trace("forward start[%-10s](%s): %s" % ('data' if l == 0 else layers_names[l-1], next_subject.shape, str(next_subject[0])[:40]))
            next_subject = layer.fprop(next_subject)
            if l < 10:
                with open("subj" + str(l), "w+") as file:
                    dump(next_subject, file)
            self.logger.debug("%s %s %s" % (l, layers_names[l], next_subject.shape))
            self.logger.trace("forward result[%-10s](%s): %s" % (layers_names[l], next_subject.shape, str(next_subject[0])[:40]))
            if "conv" not in layers_names[l]:
                self.logger.debug("next_subject[%-10s]: %s" % (layers_names[l], str(next_subject)[:60]))
            #self.logger.trace("forward start[%-10s](%s): %s" % ('data' if l == 0 else layers_names[l-1], next_style.shape, str(next_style[0])[:40]))
            next_style = layer.fprop(next_style)
            #self.logger.trace("forward result[%-10s](%s): %s" % (layers_names[l], next_style.shape, str(next_style[0])[:40]))
            self.logger.debug("next_style  [%-10s]: %s" % (layers_names[l], str(next_style)[:40]))
            if self.subject_weights[l] > 0:
                self.subject_feats[l] = next_subject
                self.logger.debug("%-2s %-8s %s" % (l, layers_names[l], next_subject.shape))
            if self.style_weights[l] > 0:
                gram = gram_matrix(next_style)
                self.logger.trace(l, layer.name, next_style.shape, str(next_style)[:60])
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

    def get_layer_name(self, layer):
        return self.layers_names_map[layer]

    @property
    def image(self):
        return np.array(self.x.array)

    @property
    def _params(self):
        return [self.x]

    def _update(self):
        # Forward propagation
        next_x = self.x.array
        # print "next_x = %s" % next_x[0][0][0][0]
        x_feats = [None] * len(self.layers)
        for l, layer in enumerate(self.layers):
            next_x = layer.fprop(next_x)
            # print "l = %s, next_x = %s" % (l, next_x[0][0][0][0])
            self.logger.debug("next_x = %s" % list(next_x.shape))
            if self.subject_weights[l] > 0 or self.style_weights[l] > 0:
                x_feats[l] = next_x

        # Backward propagation
        grad = ca.zeros_like(next_x)
        loss = ca.zeros(1)
        self.logger.trace("x_feats = %s" % list(map(lambda x: None if x is None else x.shape, x_feats)))
        # print "x_feats = %s" % list(map(lambda x: None if x is None else x.shape, x_feats))
        # print " ".join(map(lambda layer: layer.name, self.layers))
        for l, layer in reversed(list(enumerate(self.layers))):
            if self.subject_weights[l] > 0:
                self.logger.debug("shapes[%s] %s %s" % (l, x_feats[l].shape, self.subject_feats[l].shape))
                diff = x_feats[l] - self.subject_feats[l]
                norm = ca.sum(ca.fabs(diff)) + 1e-8
                weight = float(self.subject_weights[l]) / norm
                grad += diff * weight
                loss += 0.5 * weight * ca.sum(diff ** 2)
            if self.style_weights[l] > 0:
                diff = gram_matrix(x_feats[l]) - self.style_grams[l]
                # print "l = %s, style_grams = %s" % (l, self.style_grams[l].shape)
                n_channels = diff.shape[0]
                x_feat = ca.reshape(x_feats[l], (n_channels, -1))
                style_grad = ca.reshape(ca.dot(diff, x_feat), x_feats[l].shape)
                norm = ca.sum(ca.fabs(style_grad))
                weight = float(self.style_weights[l]) / norm
                style_grad *= weight
                self.logger.debug("style_grad = %s" % list(style_grad.shape))
                grad += style_grad
                loss += 0.25 * weight * ca.sum(diff ** 2)
            grad = layer.bprop(grad)
            self.logger.debug("blob = %s, grad_shape = %s, grad = %s" % (self.get_layer_name(layer), list(grad.shape),
                                                                         str(grad)[:50]))

        if self.tv_weight > 0:
            x = ca.reshape(self.x.array, (3, 1) + grad.shape[2:])
            tv = self.tv_conv.fprop(x, self.tv_kernel)
            tv *= self.tv_weight
            grad -= ca.reshape(tv, grad.shape)

        ca.copyto(self.x.grad_array, grad)
        return loss
