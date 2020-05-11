#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# @Time    : 2019/12/13 下午4:02
# @Author  : MaybeShewill-CV
# @Site    : https://github.com/MaybeShewill-CV/bisenetv2-tensorflow
# @File    : cityscapes_bisenet_trainner.py
# @IDE: PyCharm
"""
Bisenet trainner for city spaces dataset
"""
import os
import os.path as ops
import shutil
import time
import math

import numpy as np
import tensorflow as tf
import loguru
import tqdm

from local_utils.config_utils import parse_config_utils
from bisenet_model import bisenet
from data_provider import cityscapes_reader

LOG = loguru.logger
CFG = parse_config_utils.cityscapes_cfg


class BiseNetCityScapesTrainer(object):
    """
    init bisenet trainner
    """

    def __init__(self):
        """
        initialize bisenet trainner
        """
        # define solver params and dataset
        self._city_spaces_reader = cityscapes_reader.CitySpacesReader()
        self._train_dataset = self._city_spaces_reader.train_dataset
        self._val_dataset = self._city_spaces_reader.val_dataset
        self._steps_per_epoch = len(self._train_dataset)

        self._model_name = CFG.MODEL.MODEL_NAME

        self._train_epoch_nums = CFG.TRAIN.EPOCH_NUMS
        self._batch_size = CFG.TRAIN.BATCH_SIZE
        self._model_save_dir = CFG.TRAIN.MODEL_SAVE_DIR
        self._snapshot_epoch = CFG.TRAIN.SNAPSHOT_EPOCH
        self._model_save_dir = ops.join(CFG.TRAIN.MODEL_SAVE_DIR, self._model_name)
        self._tboard_save_dir = ops.join(CFG.TRAIN.TBOARD_SAVE_DIR, self._model_name)

        self._input_tensor_size = CFG.AUG.TRAIN_CROP_SIZE

        self._momentum = CFG.SOLVER.MOMENTUM
        self._init_learning_rate = CFG.SOLVER.LR
        self._moving_ave_decay = CFG.SOLVER.MOVING_AVE_DECAY
        self._lr_polynimal_decay_power = CFG.SOLVER.LR_POLYNOMIAL_POWER
        self._optimizer_mode = CFG.SOLVER.OPTIMIZER.lower()
        if CFG.TRAIN.RESTORE_FROM_SNAPSHOT.ENABLE:
            self._initial_weight = CFG.TRAIN.RESTORE_FROM_SNAPSHOT.SNAPSHOT_PATH
        else:
            self._initial_weight = None
        if CFG.TRAIN.WARM_UP.ENABLE:
            self._warmup_epoches = CFG.TRAIN.WARM_UP.EPOCH_NUMS
            self._warmup_init_learning_rate = self._init_learning_rate / 1000.0
        else:
            self._warmup_epoches = 0

        # define tensorflow session
        sess_config = tf.ConfigProto(allow_soft_placement=True)
        sess_config.gpu_options.per_process_gpu_memory_fraction = CFG.GPU.GPU_MEMORY_FRACTION
        sess_config.gpu_options.allow_growth = CFG.GPU.TF_ALLOW_GROWTH
        sess_config.gpu_options.allocator_type = 'BFC'
        self._sess = tf.Session(config=sess_config)

        # define graph input tensor
        with tf.variable_scope(name_or_scope='graph_input_node'):
            self._input_src_image = tf.placeholder(
                dtype=tf.float32,
                shape=[self._batch_size, self._input_tensor_size[1], self._input_tensor_size[0], 3],
                name='inpue_source_image'
            )
            self._input_label_image = tf.placeholder(
                dtype=tf.int64,
                shape=[self._batch_size, self._input_tensor_size[1], self._input_tensor_size[0]],
                name='input_label_image'
            )

        # define model loss
        self._model = bisenet.BiseNet(phase='train')
        loss_set = self._model.compute_loss(
            input_tensor=self._input_src_image,
            label_tensor=self._input_label_image,
            name='BiseNet',
            reuse=False
        )
        self._prediciton = self._model.inference(
            input_tensor=self._input_src_image,
            name='BiseNet',
            reuse=True
        )
        self._loss = loss_set['total_loss']
        self._principal_loss = loss_set['principal_loss']
        self._joint_loss_8 = loss_set['joint_loss_8']
        self._joint_loss_16 = loss_set['joint_loss_16']
        self._l2_loss = loss_set['l2_loss']

        # define miou
        with tf.variable_scope('miou'):
            pred = tf.reshape(self._prediciton, [-1, ])
            gt = tf.reshape(self._input_label_image, [-1, ])
            indices = tf.squeeze(tf.where(tf.less_equal(gt, CFG.DATASET.NUM_CLASSES - 1)), 1)
            gt = tf.gather(gt, indices)
            pred = tf.gather(pred, indices)
            self._miou, self._miou_update_op = tf.metrics.mean_iou(
                labels=gt,
                predictions=pred,
                num_classes=CFG.DATASET.NUM_CLASSES
            )

        # define learning rate
        with tf.variable_scope('learning_rate'):
            self._global_step = tf.Variable(1.0, dtype=tf.float32, trainable=False, name='global_step')
            warmup_steps = tf.constant(
                self._warmup_epoches * self._steps_per_epoch, dtype=tf.float32, name='warmup_steps'
            )
            train_steps = tf.constant(
                self._train_epoch_nums * self._steps_per_epoch, dtype=tf.float32, name='train_steps'
            )
            self._learn_rate = tf.cond(
                pred=self._global_step < warmup_steps,
                true_fn=lambda: self._compute_warmup_lr(warmup_steps=warmup_steps, name='warmup_lr'),
                false_fn=lambda: tf.train.polynomial_decay(
                    learning_rate=self._init_learning_rate,
                    global_step=self._global_step,
                    decay_steps=train_steps,
                    end_learning_rate=0.000001,
                    power=self._lr_polynimal_decay_power)
            )
            self._learn_rate = tf.identity(self._learn_rate, 'lr')
            global_step_update = tf.assign_add(self._global_step, 1.0)

        # define moving average op
        with tf.variable_scope(name_or_scope='moving_avg'):
            if CFG.TRAIN.FREEZE_BN.ENABLE:
                train_var_list = [
                    v for v in tf.trainable_variables() if 'beta' not in v.name and 'gamma' not in v.name
                ]
            else:
                train_var_list = tf.trainable_variables()
            moving_ave_op = tf.train.ExponentialMovingAverage(
                self._moving_ave_decay).apply(train_var_list + tf.moving_average_variables())

        # define training op
        with tf.variable_scope(name_or_scope='train_step'):
            if CFG.TRAIN.FREEZE_BN.ENABLE:
                train_var_list = [
                    v for v in tf.trainable_variables() if 'beta' not in v.name and 'gamma' not in v.name
                ]
            else:
                train_var_list = tf.trainable_variables()
            if self._optimizer_mode == 'sgd':
                optimizer = tf.train.MomentumOptimizer(
                    learning_rate=self._learn_rate,
                    momentum=self._momentum
                )
            elif self._optimizer_mode == 'adam':
                optimizer = tf.train.AdamOptimizer(
                    learning_rate=self._learn_rate,
                )
            else:
                raise ValueError('Not support optimizer: {:s}'.format(self._optimizer_mode))
            optimize_op = optimizer.minimize(self._loss, var_list=train_var_list)
            with tf.control_dependencies(tf.get_collection(tf.GraphKeys.UPDATE_OPS)):
                with tf.control_dependencies([optimize_op, global_step_update]):
                    with tf.control_dependencies([moving_ave_op]):
                        self._train_op = tf.no_op()

        # define saver and loader
        with tf.variable_scope('loader_and_saver'):
            self._net_var = [vv for vv in tf.global_variables() if 'lr' not in vv.name]
            self._loader = tf.train.Saver(self._net_var)
            self._saver = tf.train.Saver(tf.global_variables(), max_to_keep=5)

        # define summary
        with tf.variable_scope('summary'):
            tf.summary.scalar("learn_rate", self._learn_rate)
            tf.summary.scalar("total", self._loss)
            tf.summary.scalar("principal", self._principal_loss)
            tf.summary.scalar("joint_loss_8", self._joint_loss_8)
            tf.summary.scalar("joint_loss_16", self._joint_loss_16)
            tf.summary.scalar('l2_loss', self._l2_loss)
            with tf.control_dependencies([self._miou_update_op]):
                tf.summary.scalar('miou', self._miou)
            if ops.exists(self._tboard_save_dir):
                shutil.rmtree(self._tboard_save_dir)
            os.makedirs(self._tboard_save_dir, exist_ok=True)
            model_params_file_save_path = ops.join(self._tboard_save_dir, CFG.TRAIN.MODEL_PARAMS_CONFIG_FILE_NAME)
            with open(model_params_file_save_path, 'w', encoding='utf-8') as f_obj:
                CFG.dump_to_json_file(f_obj)
            self._write_summary_op = tf.summary.merge_all()
            self._summary_writer = tf.summary.FileWriter(self._tboard_save_dir, graph=self._sess.graph)

        LOG.info('Initialize cityscapes bisenet trainner complete')

    def _compute_warmup_lr(self, warmup_steps, name):
        """

        :param warmup_steps:
        :param name:
        :return:
        """
        with tf.variable_scope(name_or_scope=name):
            factor = tf.math.pow(self._init_learning_rate / self._warmup_init_learning_rate, 1.0 / warmup_steps)
            warmup_lr = self._warmup_init_learning_rate * tf.math.pow(factor, self._global_step)
        return warmup_lr

    def train(self):
        """

        :return:
        """
        self._sess.run(tf.global_variables_initializer())
        self._sess.run(tf.local_variables_initializer())
        if CFG.TRAIN.RESTORE_FROM_SNAPSHOT.ENABLE:
            try:
                LOG.info('=> Restoring weights from: {:s} ... '.format(self._initial_weight))
                self._loader.restore(self._sess, self._initial_weight)
                global_step_value = self._sess.run(self._global_step)
                remain_epoch_nums = self._train_epoch_nums - math.floor(global_step_value / self._steps_per_epoch)
                epoch_start_pt = self._train_epoch_nums - remain_epoch_nums
            except OSError as e:
                LOG.error(e)
                LOG.info('=> {:s} does not exist !!!'.format(self._initial_weight))
                LOG.info('=> Now it starts to train BiseNet from scratch ...')
                epoch_start_pt = 1
            except Exception as e:
                LOG.error(e)
                LOG.info('=> Can not load pretrained model weights: {:s}'.format(self._initial_weight))
                LOG.info('=> Now it starts to train BiseNet from scratch ...')
                epoch_start_pt = 1
        else:
            LOG.info('=> Starts to train BiseNet from scratch ...')
            epoch_start_pt = 1

        for epoch in range(epoch_start_pt, self._train_epoch_nums):
            traindataset_pbar = tqdm.tqdm(self._train_dataset)
            train_epoch_losses = []
            train_epoch_mious = []
            val_epoch_losses = []
            val_epoch_mious = []

            for samples in traindataset_pbar:
                source_imgs = samples[0]
                label_imgs = samples[1]

                _, _, summary, train_step_loss, global_step_val = self._sess.run(
                    fetches=[self._train_op, self._miou_update_op, self._write_summary_op,
                             self._loss, self._global_step],
                    feed_dict={
                        self._input_src_image: source_imgs,
                        self._input_label_image: label_imgs,
                    }
                )
                train_step_miou = self._sess.run(
                    fetches=self._miou
                )
                train_epoch_losses.append(train_step_loss)
                train_epoch_mious.append(train_step_miou)
                self._summary_writer.add_summary(summary, global_step=global_step_val)
                traindataset_pbar.set_description(
                    'train loss: {:.5f}, miou: {:.5f}'.format(train_step_loss, train_step_miou)
                )

            for samples in self._val_dataset:
                source_imgs = samples[0]
                label_imgs = samples[1]
                _, val_step_loss = self._sess.run(
                    [self._miou_update_op, self._loss],
                    feed_dict={
                        self._input_src_image: source_imgs,
                        self._input_label_image: label_imgs,
                    }
                )
                val_step_miou = self._sess.run(
                    fetches=self._miou
                )
                val_epoch_losses.append(val_step_loss)
                val_epoch_mious.append(val_step_miou)

            train_epoch_losses = np.mean(train_epoch_losses)
            val_epoch_losses = np.mean(val_epoch_losses)
            train_epoch_mious = np.mean(train_epoch_mious)
            val_epoch_mious = np.mean(val_epoch_mious)

            if epoch % self._snapshot_epoch == 0:
                snapshot_model_name = 'city_spaces_val_miou={:.4f}.ckpt'.format(val_epoch_mious)
                snapshot_model_path = ops.join(self._model_save_dir, snapshot_model_name)
                os.makedirs(self._model_save_dir, exist_ok=True)
                self._saver.save(self._sess, snapshot_model_path, global_step=epoch)

            log_time = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))
            LOG.info(
                '=> Epoch: {:d} Time: {:s} Train loss: {:.5f} Test loss: {:.5f} '
                'Train miou: {:.5f} Test miou: {:.5f} ...'.format(
                    epoch, log_time,
                    train_epoch_losses, val_epoch_losses,
                    train_epoch_mious, val_epoch_mious
                )
            )
        LOG.info('Complete training process good luck!!')

        return
