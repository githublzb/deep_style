import numpy as np
import caffe
from style_parameter import StyleParameter
from math import ceil
from debug_logger import Logger
from copy import deepcopy


def gram_matrix(img_bc01):
    n_channels = img_bc01.shape[1]
    feats = np.reshape(img_bc01, (n_channels, -1))
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
                 layers=None, init_img=None, mean=None, channel_swap=None, init_noise=0.0):

        caffe.Net.__init__(self, prototxt, params_file, caffe.TEST)
        self.logger = Logger("caffe_style", True, 0)

        self.input_name = self._blob_names[0]

        # configure pre-processing
        in_ = self.inputs[0]
        style_tramsformer = caffe.io.Transformer(
            {in_: (1,) + tuple(np.roll(style_img.shape, 1))})
        self.transformer = caffe.io.Transformer(
            {in_: (1,) + tuple(np.roll(subject_img.shape, 1))})

        style_tramsformer.set_transpose(in_, (2, 0, 1))
        self.transformer.set_transpose(in_, (2, 0, 1))

        if mean is not None:
            style_tramsformer.set_mean(in_, mean)
            self.transformer.set_mean(in_, mean)
        if channel_swap is not None:
            style_tramsformer.set_channel_swap(in_, channel_swap)
            self.transformer.set_channel_swap(in_, channel_swap)

        # the reference model operates on images in [0,255] range instead of
        # [0,1]
        style_tramsformer.set_raw_scale(in_, 255)
        self.transformer.set_raw_scale(in_, 255)

        self.crop_dims = np.array(self.blobs[in_].data.shape[2:])
        self.image_dims = self.crop_dims

        if layers is None:
            layers = self.layers

        # Map weights (in convolution indices) to layer indices
        subject_weights = weight_array(subject_weights) * subject_ratio
        style_weights = weight_array(style_weights)
        self.subject_weights = np.zeros(len(layers))
        self.style_weights = np.zeros(len(layers))
        layers_len = 0
        conv_idx = 0
        for l, layer in enumerate(layers):
            if layer.type == "InnerProduct":
                break
            if layer.type == "ReLU":
                self.subject_weights[l] = subject_weights[conv_idx]
                self.style_weights[l] = style_weights[conv_idx]
                if subject_weights[conv_idx] > 0 or \
                                style_weights[conv_idx] > 0:
                    layers_len = l + 1
                conv_idx += 1

        subject_img = self.transformer.preprocess(
            self.input_name, subject_img)[np.newaxis, ...]

        style_img = style_tramsformer.preprocess(
            self.input_name, style_img)[np.newaxis, ...]
        init_img = subject_img
        noise = np.random.normal(
            size=init_img.shape, scale=np.std(init_img) * 1e-1)
        init_img = init_img * (1 - init_noise) + noise * init_noise

        # Discard unused layers
        layers = layers[:layers_len]
        self._layers = layers

        # Setup network
        x_shape = init_img.shape
        self.x = StyleParameter(init_img)
        self.x._setup(x_shape)
        self.x._array = np.array(self.x._array)

        self.reshape_from_to(x_shape, self.input_name, "")

        # Precompute subject features and style Gram matrices
        self.subject_feats = [None] * len(layers)
        self.style_grams = [None] * len(layers)

        blobs_name_list = self.blobs.keys()

        layer_idx = 0
        curr_blob_name = blobs_name_list[layer_idx]
        next_blob_name = blobs_name_list[layer_idx + 1]

        blob_start = self.blobs[curr_blob_name]
        h, w = subject_img[0].shape[-2:]
        subj_shape = (blob_start.num, blob_start.channels, h, w)
        h, w = style_img[0].shape[-2:]
        style_shape = (blob_start.num, blob_start.channels, h, w)

        next_subject = subject_img
        next_style = style_img

        next_subjects = []
        layer_idx = - 1
        for l, layer in enumerate(layers):
            if layer.type == "InnerProduct":
                break
            if layer.type != "ReLU":
                layer_idx += 1
            if layer.type == "Convolution":
                continue

            curr_layer_name = self._layer_names[l]

            if layer_idx < len(self.blobs):
                curr_blob_name = blobs_name_list[layer_idx]
                next_blob_name = blobs_name_list[layer_idx + 1]

                if "fc" not in next_blob_name and "fc" not in curr_blob_name:
                    self.logger.debug("%s %s %s %s" % (l, curr_blob_name, next_blob_name, curr_layer_name))
                    self.logger.trace("forward start[%-10s](%s): %s" % ('data' if l == 0 else self._layer_names[l - 1],
                                                                        next_subject.shape, str(next_subject[0])[:40]))
                    next_subject = deepcopy(self.fprop(subj_shape,
                                                       curr_blob_name, next_blob_name, curr_layer_name, next_subject))

                    self.logger.trace("forward result[%-10s](%s): %s" % (self._layer_names[l],
                                                                         next_subject.shape, str(next_subject[0])[:40]))
                    self.logger.trace("%s %s %s" % (l, curr_layer_name, next_subject.shape))
                    next_subjects.append(next_subject)
                    self.logger.debug("next_subject[%-10s]: %s" % (curr_layer_name, str(next_subject)[:60]))
                    self.logger.trace("forward start[%-10s](%s): %s" % ('data' if l == 0 else self._layer_names[l - 1],
                                                                        next_style.shape, str(next_style[0])[:40]))
                    self.blobs[curr_blob_name].mutable_cpu_data()
                    next_style = deepcopy(self.fprop(style_shape,
                                                     curr_blob_name, next_blob_name, curr_layer_name, next_style))
                    self.logger.trace("forward result[%-10s](%s): %s" % (self._layer_names[l], next_style.shape,
                                                                         str(next_style[0])[:40]))

                    self.logger.debug("next_style  [%-10s]: %s" % (next_blob_name, str(next_style)[:40]))

            if self.subject_weights[l] > 0:
                curr_blob_name = blobs_name_list[layer_idx]
                result_subj = deepcopy(next_subject)
                self.logger.debug("%-2s %-8s %s" % (l, curr_blob_name, next_subject.shape))
                self.subject_feats[l] = result_subj
            if self.style_weights[l] > 0:
                result_style = deepcopy(next_style)
                gram = gram_matrix(result_style)
                self.logger.trace(l, curr_blob_name, curr_layer_name, list(result_style.shape), str(result_style)[:60])
                # Scale gram matrix to compensate for different image sizes
                n_pixels_subject = np.prod(result_style.shape[2:])
                n_pixels_style = np.prod(result_style.shape[2:])
                scale = (n_pixels_subject / float(n_pixels_style))
                self.style_grams[l] = gram * scale

    def fprop(self, shape, blob_name_start, blob_name_end, curr_layer_name, data):
        self.blobs[self.input_name].reshape(shape[0], shape[1], shape[2], shape[3])
        blob_start = self.blobs[blob_name_start]
        self.reshape_from_to(shape, self.input_name, blob_name_end)
        self.logger.debug("forward start[%-10s](%s): %s" % (blob_name_start, data.shape, str(data[0])[:40]))
        blob_start.data[...] = data[0]
        if "pool" in blob_name_start:
            blob_name_start = blob_name_end
        if blob_name_start == self.input_name:
            result = self.forward(end=curr_layer_name)[curr_layer_name]
        else:
            start_layer_name = curr_layer_name
            if "relu" in start_layer_name:
                start_layer_name = 'conv' + start_layer_name[-3:]
            result = self.forward(start=start_layer_name, end=curr_layer_name)[curr_layer_name]
        self.logger.debug("forward result[%-10s](%s): %s" % (curr_layer_name, result.shape, str(result[0])[:40]))
        return result

    def bprop(self, blob_name_start, blob_name_end, curr_layer_name, data):
        if len(data[0].shape[-2:]) < 2:
            raise Exception()
        blob_end = self.blobs[blob_name_end]
        blob_end.diff[...] = data[0]
        if "pool" in blob_name_start:
            blob_name_start = blob_name_end
        if blob_name_start == self.input_name:
            result = self.backward(start=blob_name_end)[blob_name_start]
        else:
            start_layer_name = curr_layer_name
            if "relu" in start_layer_name:
                start_layer_name = 'conv' + start_layer_name[-3:]
            result = self.backward(start=curr_layer_name, end=start_layer_name)
            result = result.values()[0]
        return result

    def reshape_from_to(self, x_shape, start_blob_name, end_blob_name):
        def output_shape(blob, x_shape, channels_n):
            b, _, img_h, img_w = x_shape
            filter_shape = (3, 3)
            padding_w = 1
            padding_h = 1
            strides_w = 1
            strides_h = 1
            out_shape = ((img_h + 2 * padding_h - filter_shape[0]) //
                         strides_h + 1,
                         (img_w + 2 * padding_w - filter_shape[1]) //
                         strides_w + 1)
            return (b, channels_n) + out_shape

        if len(x_shape) < 4:
            x_shape = (1, x_shape[0], x_shape[1], x_shape[2])
        started = False
        for i, blob in enumerate(self.blobs.values()):
            blob_name = self._blob_names[i]
            if blob_name == start_blob_name:
                started = True
            if not started:
                continue
            if "_1" in blob_name:
                shape = blob.shape
                blob.reshape(shape[0], shape[1], x_shape[2], x_shape[3])
                x_shape = output_shape(blob, x_shape, blob.channels)
            elif "_2" in blob_name:
                shape = blob.shape
                blob.reshape(shape[0], shape[1], x_shape[2], x_shape[3])
                x_shape = (shape[0], shape[1]) + x_shape[2:]
            elif "pool" in blob_name:
                shape = blob.shape
                x_shape = (shape[0], shape[1]) + x_shape[2:]
                x_shape = (shape[0], shape[1]) + \
                          (int(ceil(x_shape[2] / 2.0)), int(ceil(x_shape[3] / 2.0)))
                blob.reshape(shape[0], shape[1], x_shape[2], x_shape[3])
            elif self.input_name in blob_name:
                shape = blob.shape
                blob.reshape(shape[0], shape[1], x_shape[2], x_shape[3])
                x_shape = output_shape(
                    blob, x_shape, self.blobs.values()[1].channels)
            else:
                shape = blob.shape
                blob.reshape(shape[0], shape[1], x_shape[2], x_shape[3])
                x_shape = output_shape(blob, x_shape, blob.channels)
            if blob_name == end_blob_name:
                break

    @property
    def image(self):
        return np.array(self.x.array)

    @property
    def _params(self):
        return [self.x]

    @property
    def reduced_layers(self):
        return self._layers

    def update(self):
        blobs_name_list = self.blobs.keys()

        # Forward propagation
        next_x = self.x.array
        subj_shape = next_x.shape
        self.logger.debug("next_x = %s" % next_x[0][0][0][0])
        x_feats = [None] * len(self.reduced_layers)
        last_blob_name = self.blobs.keys()[0]
        blob_name = self.blobs.keys()[1]
        next_x = self.fprop(subj_shape, last_blob_name, blob_name, blob_name, next_x)
        layer_idx = -1
        for l, layer in enumerate(self.reduced_layers):
            if layer.type == "InnerProduct":
                break
            if layer.type != "ReLU":
                layer_idx += 1
            if layer.type == "Convolution":
                continue

            curr_layer_name = self._layer_names[l]

            if l > 0 and layer_idx < len(self.blobs):
                curr_blob_name = blobs_name_list[layer_idx]
                next_blob_name = blobs_name_list[layer_idx + 1]
                if layer_idx == 0:
                    curr_blob_name = next_blob_name

                if "fc" not in next_blob_name and "fc" not in curr_blob_name and "pool5" not in next_blob_name:
                    next_x = self.fprop(subj_shape, curr_blob_name, next_blob_name, curr_layer_name, next_x)
                    self.logger.debug("next_x = %s" % list(next_x.shape))

            if self.subject_weights[l] > 0 or self.style_weights[l] > 0:
                curr_blob_name = blobs_name_list[layer_idx + 1]
                result_subj = self.blobs[curr_blob_name].data
                x_feats[l] = result_subj

        # Backward propagation
        grad = np.zeros_like(next_x)
        loss = np.zeros(1)

        self.logger.debug(" ".join(map(lambda layer: layer.type, self.reduced_layers)))
        self.logger.trace("x_feats = %s" % list(map(lambda x: None if x is None else x.shape, x_feats)))
        for l, style_gram in enumerate(self.style_grams):
            if style_gram is not None:
                self.logger.debug("l = %s, style_grams = %s" % (l, style_gram.shape))

        layer_idx = 17
        for l, layer in reversed(list(enumerate(self.reduced_layers))):
            curr_layer_name = self._layer_names[l]
            if layer.type != "Convolution":
                layer_idx -= 1
            if self.subject_weights[l] > 0:
                self.logger.debug("shapes[%s] %s %s" % (l, x_feats[l].shape, self.subject_feats[l].shape))
                diff = x_feats[l] - self.subject_feats[l]
                norm = np.sum(np.fabs(diff)) + 1e-8
                weight = float(self.subject_weights[l]) / norm
                grad += diff * weight
                loss += 0.5 * weight * np.sum(diff ** 2)
            if self.style_weights[l] > 0:
                diff = gram_matrix(x_feats[l]) - self.style_grams[l]
                n_channels = diff.shape[0]
                x_feat = np.reshape(x_feats[l], (n_channels, -1))
                style_grad = np.reshape(np.dot(diff, x_feat), x_feats[l].shape)
                norm = np.sum(np.fabs(style_grad))
                weight = float(self.style_weights[l]) / norm
                style_grad *= weight
                self.logger.debug("style_grad = %s" % list(style_grad.shape))
                grad += style_grad
                loss += 0.25 * weight * np.sum(diff ** 2)
            if layer.type != "Convolution":
                self.logger.debug("blob = %s, grad_shape = %s, grad = %s" % (curr_layer_name, list(grad.shape),
                                                                             str(grad)[:50]))
                grad = self.bprop(blobs_name_list[layer_idx], blobs_name_list[layer_idx + 1], curr_layer_name, grad)

        np.copyto(self.x.grad_array, grad)
        return loss
