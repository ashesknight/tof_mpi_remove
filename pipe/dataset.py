import sys

sys.path.insert(0, '../sim/')
import numpy as np
import tensorflow as tf
import os, json, glob
import imageio
import matplotlib
import math
# matplotlib.use('Agg')
import matplotlib.pyplot as plt
from mpl_toolkits.mplot3d import Axes3D
from utils import *
from tof_class import *
import pdb
import pickle
import time
import scipy.misc
from scipy import sparse
import scipy.interpolate
from copy import deepcopy
from joblib import Parallel, delayed
import multiprocessing
from kinect_spec import *
import cv2
from numpy import linalg as LA

from tensorflow.contrib import learn
from tensorflow.contrib.learn.python.learn.estimators import model_fn as model_fn_lib

from kinect_option import kinect_mask_tensor

tf.logging.set_verbosity(tf.logging.INFO)
from kinect_init import *

tof_cam = kinect_real_tf()

PI = 3.14159265358979323846
flg = False
dtype = tf.float32


def colorize_img(value, vmin=None, vmax=None, cmap=None):
    """
    A utility function for TensorFlow that maps a grayscale image to a matplotlib colormap for use with TensorBoard image summaries.
    By default it will normalize the input value to the range 0..1 before mapping to a grayscale colormap.
    Arguments:
      - value: 4D Tensor of shape [batch_size,height, width,1]
      - vmin: the minimum value of the range used for normalization. (Default: value minimum)
      - vmax: the maximum value of the range used for normalization. (Default: value maximum)
      - cmap: a valid cmap named for use with matplotlib's 'get_cmap'.(Default: 'gray')

    Returns a 3D tensor of shape [batch_size,height, width,3].
    """

    # normalize
    vmin = tf.reduce_min(value) if vmin is None else vmin
    vmax = tf.reduce_max(value) if vmax is None else vmax
    value = (value - vmin) / (vmax - vmin)  # vmin..vmax

    # quantize
    indices = tf.to_int32(tf.round(value[:, :, :, 0] * 255))

    # gather
    color_map = matplotlib.cm.get_cmap(cmap if cmap is not None else 'gray')
    colors = color_map(np.arange(256))[:, :3]
    colors = tf.constant(colors, dtype=tf.float32)
    value = tf.gather(colors, indices)
    return value

def preprocessing(features, labels):
    msk = kinect_mask_tensor()
    meas = features['full']
    meas = [meas[:, :, i] * msk / tof_cam.cam['map_max'] for i in
            range(meas.shape[-1])]  ##tof_cam.cam['map_max'] == 3500
    meas = tf.stack(meas, -1)
    meas_p = meas[20:-20, :, :]

    ideal = labels['ideal']
    ideal = [ideal[:, :, i] * msk / tof_cam.cam['map_max'] for i in range(ideal.shape[-1])]
    ideal = tf.stack(ideal, -1)
    ideal_p = ideal[20:-20, :, :]
    gt = labels['gt']
    gt = tf.image.resize_images(gt, [meas.shape[0], meas.shape[1]])
    gt = tof_cam.dist_to_depth(gt)
    gt_p = gt[20:-20, :, :]
    features['full'] = meas_p
    labels['ideal'] = ideal_p
    labels['gt'] = gt_p
    return features, labels

def imgs_input_fn(filenames, height, width, shuffle=False, repeat_count=1, batch_size=32):
    def _parse_function(serialized, height=height, width=width):
        features = \
            {
                'meas': tf.FixedLenFeature([], tf.string),
                'gt': tf.FixedLenFeature([], tf.string),
                'ideal': tf.FixedLenFeature([], tf.string)
            }

        parsed_example = tf.parse_single_example(serialized=serialized, features=features)

        meas_shape = tf.stack([height, width, 9])
        gt_shape = tf.stack([height * 4, width * 4, 1])
        ideal_shape = tf.stack([height, width, 9])

        meas_raw = parsed_example['meas']
        gt_raw = parsed_example['gt']
        ideal_raw = parsed_example['ideal']

        # decode the raw bytes so it becomes a tensor with type

        meas = tf.decode_raw(meas_raw, tf.int32)
        meas = tf.cast(meas, tf.float32)
        meas = tf.reshape(meas, meas_shape)

        gt = tf.decode_raw(gt_raw, tf.float32)
        gt = tf.reshape(gt, gt_shape)

        ideal = tf.decode_raw(ideal_raw, tf.int32)
        ideal = tf.cast(ideal, tf.float32)
        ideal = tf.reshape(ideal, ideal_shape)

        features = {'full': meas}
        labels = {'gt': gt, 'ideal': ideal}

        return features, labels

    dataset = tf.data.TFRecordDataset(filenames=filenames)
    # Parse the serialised data to TFRecords files.
    # returns Tensorflow tensors for the image and labels.
    dataset = dataset.map(_parse_function)
    dataset = dataset.map(
        lambda features, labels: preprocessing(features, labels)
    )

    if shuffle:
        dataset = dataset.shuffle(buffer_size=256)

    dataset = dataset.repeat(repeat_count)  # Repeat the dataset this time
    batch_dataset = dataset.batch(batch_size)  # Batch Size
    iterator = batch_dataset.make_one_shot_iterator()  # Make an iterator
    batch_features, batch_labels = iterator.get_next()  # Tensors to get next batch of image and their labels

    return batch_features, batch_labels

def imgs_input_fn_inverse(filenames, height, width, shuffle=False, repeat_count=1, batch_size=32):
    batch_features, batch_labels = imgs_input_fn(filenames, height, width, shuffle=False, repeat_count=1, batch_size=32)
    return batch_labels, batch_features

def dof_computer(dist, samples, batch_size, z_multiplier, coords_h_pos, coords_w_pos):
    N = samples.shape.as_list()[-1]
    XX_s, YY_s, ZZ_s = map2mesh_samples(samples, tof_cam.cam, batch_size, z_multiplier, yy_coords=coords_h_pos, xx_coords=coords_w_pos)
    XX, YY, ZZ = map2mesh(dist, tof_cam.cam, batch_size, z_multiplier)
    XX = tf.tile(XX, multiples=[1,1,1,N])
    YY = tf.tile(YY, multiples=[1, 1, 1, N])
    ZZ = tf.tile(ZZ, multiples=[1, 1, 1, N])
    dist = tf.tile(dist, multiples=[1, 1, 1, N])
    dof_samp_cur = tf.sqrt((XX-XX_s)**2 + (YY-YY_s)**2 + (ZZ-ZZ_s)**2)
    dof_samples = dof_samp_cur + samples + dist
    return dof_samples