import os
import cv2
import random
import argparse
import torchaudio
import torch.nn.functional as F
from models.tf.tf_common import *
import torch
import torch.nn as nn
from mmcv.runner import load_checkpoint
from mmcv import Config

# gpus = tf.config.experimental.list_physical_devices(device_type='GPU')
# if len(gpus) != 0:
#     tf.config.experimental.set_virtual_device_configuration(gpus[0],
#                                                             [tf.config.experimental.VirtualDeviceConfiguration(
#                                                                 memory_limit=8192)])
#     tf.config.experimental.set_memory_growth(gpus[0], True)
# else:
#     tf.config.experimental.set_visible_devices([], 'GPU')
tf.config.experimental.set_visible_devices([], 'GPU')


def parse_args():
    parser = argparse.ArgumentParser(description='Convert PyTorch to TFLite')
    parser.add_argument('type', default='mmdet', help='Choose training type')
    parser.add_argument('config', type=str, help='test config file path')
    parser.add_argument('--weights', type=str, help='torch model file path')
    parser.add_argument('--tflite_type', type=str, default='int8', help='Quantization type for tflite, '
                                                                        '(int8, fp16, fp32)')
    parser.add_argument('--data', type=str, help='Representative dataset path, need at least 100 images')
    parser.add_argument('--audio', action='store_true', help='Choose audio dataset load code if given')
    # parser.add_argument('--shape', type=int, nargs='+', default=[112], help='input data size')
    # parser.add_argument('--save', type=str, help='Tflite model path')

    args = parser.parse_args()

    return args


def representative_dataset_2d(files, size):
    for i, fn in enumerate(files):
        img = cv2.imread(fn)
        img = cv2.resize(img, (size[1], size[0]))[:, :, ::-1]
        img = np.expand_dims(img, axis=0).astype(np.float32)
        img /= 255.0

        yield [img]
        if i >= 100:
            break


def representative_dataset_1d(files, size):
    for i, fn in enumerate(files):
        wave, sr = torchaudio.load(fn, normalize=True)
        wave = torchaudio.transforms.Resample(sr, 8000)(wave)
        wave.squeeze_()
        wave = (wave / wave.__abs__().max()).float()
        if wave.shape[0] >= size:
            max_audio_start = wave.size(0) - size
            audio_start = random.randint(0, max_audio_start)
            wave = wave[audio_start:audio_start + size]
        else:
            wave = F.pad(wave, (0, size - wave.size(0)),
                         "constant").data

        yield [wave[None, :, None]]
        if i > 100:
            break


def graph2dict(model):
    """Generating tf dictionary from torch graph."""

    gm: torch.fx.GraphModule = torch.fx.symbolic_trace(model, concrete_args={'flag': True})
    # gm.graph.print_tabular()
    modules = dict(model.named_modules())

    tf_dict = dict()
    for node in gm.graph.nodes:
        if node.op == 'get_attr':
            tf_dict[node.name] = modules[node.target.rsplit('.', 1)[0]]  # Audio model
        if node.op == 'call_module':
            op = modules[node.target]
            if isinstance(op, (nn.Conv2d, nn.Conv1d)):
                tf_dict[node.name] = eval(f'TFBase{op.__class__.__name__}')(op)
            if isinstance(op, (nn.BatchNorm2d, nn.BatchNorm1d)):
                tf_dict[node.name] = TFBN(op)

            if isinstance(op, (nn.ReLU, nn.ReLU6, nn.LeakyReLU, nn.Sigmoid)):
                tf_dict[node.name] = TFActivation(op)
            if 'drop_path' in node.name:  # yolov3
                tf_dict[node.name] = tf.identity
            if isinstance(op, nn.Dropout):
                tf_dict[node.name] = keras.layers.Dropout(rate=op.p)

            if isinstance(op, (nn.MaxPool2d, nn.AvgPool2d, nn.AdaptiveAvgPool1d, nn.AdaptiveAvgPool2d)):
                tf_dict[node.name] = tf_pool(op)
            if isinstance(op, nn.Linear):
                tf_dict[node.name] = TFDense(op)
            if isinstance(op, nn.Softmax) or node.name == 'sm':
                tf_dict[node.name] = keras.layers.Softmax(axis=-1)
            if isinstance(op, nn.Upsample):
                # tf_dict[node.name] = keras.layers.UpSampling2D(size=int(op.scale_factor), interpolation=op.mode)
                tf_dict[node.name] = TFUpsample(op)
        if node.op in ['call_function']:
            if 'add' in node.name:
                tf_dict[node.name] = keras.layers.Add()
                # tf_dict[node.name] = lambda y: keras.layers.add([eval(str(x)) for x in y.args])
            if 'cat' in node.name:
                dim = node.args[1] if len(node.args) == 2 else node.kwargs['dim']
                dim = -1 if dim == 1 else dim - 1
                tf_dict[node.name] = keras.layers.Concatenate(axis=dim)
                # tf_dict[node.name] = lambda y: tf.concat([eval(str(x)) for x in y.args[0]], axis=dim)
            if 'conv1d' in node.name:  # audio model
                tf_dict[node.name] = TFAADownsample(tf_dict[str(node.args[1])])
            if 'interpolate' in node.name:
                tf_dict[node.name] = TFUpsample(node)

    return tf_dict, gm


class ModelParser(keras.layers.Layer):
    def __init__(self, tf_dict, gm):
        super().__init__()
        self.nodes = gm.graph.nodes
        self.tf = tf_dict

    def call(self, inputs):
        for node in self.nodes:
            if node.op == 'get_attr':  # Fixed weight, pass
                continue
            if node.op == 'placeholder':
                globals()[node.name] = inputs
            if node.name in self.tf.keys():
                if 'add' in node.name:
                    globals()[node.name] = self.tf[node.name]([eval(str(x)) for x in node.args])
                elif 'cat' in node.name:
                    globals()[node.name] = self.tf[node.name]([eval(str(x)) for x in node.args[0]])
                else:
                    globals()[node.name] = self.tf[node.name](eval(str(node.args[0])))
            if 'size' in node.name:
                if len(node.args) == 2:
                    # dim = -1 if node.args[1] == 1 else node.args[1]
                    globals()[node.name] = eval(str(node.args[0])).shape[-1]
                else:
                    n, h, w, c = eval(str(node.args[0])).shape.as_list()
                    globals()[node.name] = [n, c, h, w]
            if 'getitem' in node.name:
                globals()[node.name] = eval(str(node.args[0]))[node.args[1]]
            if 'floordiv' in node.name:
                globals()[node.name] = eval(str(node.args[0])) // 2
            if 'view' in node.name:
                if len(node.args[1:]) == 2:
                    s = eval(str(node.args[1]))
                    globals()[node.name] = tf.reshape(eval(str(node.args[0])), [-1, s])
                else:
                    if 'contiguous' in str(node.args[0]):
                        n, c, h, w = [eval(str(a)) for a in node.args[1:]]
                        globals()[node.name] = tf.reshape((eval(str(node.args[0]))), [-1, h, w, c])
                    else:
                        n, group, f, h, w = [a if isinstance(a, int) else eval(str(a)) for a in node.args[1:]]
                        globals()[node.name] = tf.reshape((eval(str(node.args[0]))), [-1, h, w, group, f])
            if 'transpose' in node.name:
                globals()[node.name] = tf.transpose(eval(str(node.args[0])), perm=[0, 1, 2, 4, 3])
            if 'contiguous' in node.name:
                globals()[node.name] = eval(str(node.args[0]))
            if 'mul' in node.name:
                globals()[node.name] = node.args[0] * eval(str(node.args[1]))
            if 'chunk' in node.name:
                globals()[node.name] = tf.split(eval(str(node.args[0])), node.args[1], axis=-1)

            if 'flatten' in node.name:
                globals()[node.name] = tf.keras.layers.Flatten()(eval(str(node.args[0])))
            if node.name == 'output':
                return eval(str(node.args[0]))
            # for x in ['size', 'getitem', 'floordiv', 'contiguous', 'mul']:
            #     if x in node.name:
            #         globals()[node.name] = tf_method(node)


def tflite(keras_model, type, data, audio):
    converter = tf.lite.TFLiteConverter.from_keras_model(keras_model)

    if type == 'fp16':
        converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS]
        converter.optimizations = [tf.lite.Optimize.DEFAULT]
        converter.target_spec.supported_types = [tf.float16]

    if type == 'int8':
        if not audio:
            input_shape = (keras_model.inputs[0].shape[1], keras_model.inputs[0].shape[2])
            converter.representative_dataset = lambda: representative_dataset_2d(data, input_shape)
        else:
            input_shape = keras_model.inputs[0].shape[1]
            converter.representative_dataset = lambda: representative_dataset_1d(data, input_shape)

        converter.optimizations = [tf.lite.Optimize.DEFAULT]
        converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
        converter.target_spec.supported_types = []
        converter.inference_input_type = tf.int8
        converter.inference_output_type = tf.int8
        converter.experimental_new_converter = True

    tflite_model = converter.convert()
    return tflite_model


def main(args):
    weights = os.path.abspath(args.weights)
    f = str(weights).replace('.pth', f'_{args.tflite_type}_test.tflite')
    cfg = Config.fromfile(args.config)
    cfg.model.pretrained = None

    # Get representive datasets
    if args.type == 'mmdet':
        from mmdet.models import build_detector as build_model
        from mmdet.datasets import build_dataset, build_dataloader

        shape = cfg.test_pipeline[1].img_scale
        shape = datasets.pipeline.transforms[1]
        files = [os.path.join(datasets[0].data_root, s['filename']) for s in datasets[0].data_infos]
    elif args.type == 'mmcls':
        from mmcls.models import build_classifier as build_model
        from mmcls.datasets import build_dataset, build_dataloader

        shape = cfg.data.val.segment_length
        datasets = [build_dataset(cfg.data.val)]
        files = datasets[0].audio_files
    else:
        from mmpose.models import build_posenet as build_model
        from mmpose.datasets import build_dataset, build_dataloader

        shape = [cfg.val_pipeline[0].height, cfg.val_pipeline[0].width]
        datasets = [build_dataset(cfg.data.val)]
        files = [os.path.join(datasets[0].data_root, s.split(' ')[0]) for s in datasets[0].lines]

    # build dataloader
    # dataset = build_dataset(cfg.data.val)
    # img = datasets[0]['img']
    # print(img[0].data.shape)
    # data_loader = build_dataloader(datasets, workers_per_gpu=1, samples_per_gpu=1, shuffle=False, drop_last=False)
    # shape = dataset.segment_length if args.audio else dataset[0]['hw']
    # print(dataset[0])
    # print(dataset[0]['img'][0].data.shape)

    # build model
    model = build_model(cfg.model)
    load_checkpoint(model, weights, map_location='cpu')
    model.cpu().eval()

    tf_dict, gm = graph2dict(model)
    mm = ModelParser(tf_dict, gm)
    inputs = tf.keras.Input(shape=(shape, 1)) if args.audio else tf.keras.Input(shape=(*shape, 3))
    outputs = mm(inputs)
    keras_model = tf.keras.Model(inputs=inputs, outputs=outputs)
    keras_model.trainable = False
    keras_model.summary()

    tflite_model = tflite(keras_model, args.tflite_type, files, args.audio)
    open(f, "wb").write(tflite_model)
    print(f'TFlite export sucess, saved as {f}')


if __name__ == '__main__':
    args = parse_args()
    main(args)