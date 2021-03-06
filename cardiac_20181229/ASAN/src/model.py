import tensorflow as tf
from keras.models import Model
from keras import layers
from keras.layers import Dense, Activation, Flatten, Lambda, LeakyReLU, Multiply, Reshape, ThresholdedReLU
from keras_contrib.layers import InstanceNormalization
from keras.layers import Input, Conv3D, MaxPooling3D, UpSampling3D, ZeroPadding3D, Cropping3D, Conv3DTranspose, \
    GlobalAveragePooling3D
from keras.optimizers import Adam
from keras.callbacks import ModelCheckpoint, LearningRateScheduler
from keras.layers import BatchNormalization, GaussianNoise
from keras.layers import concatenate
from keras.preprocessing import sequence
from keras import backend as K



def average_dice_coef(y_true, y_pred):
    loss = 0
    label_length = y_pred.get_shape().as_list()[-1]
    print(label_length)
    for num_label in range(label_length):
        y_true_f = K.flatten(y_true[..., num_label])
        y_pred_f = K.flatten(y_pred[..., num_label])
        intersection = K.sum(y_true_f * y_pred_f)
        loss += (2. * intersection + 1.) / (K.sum(y_true_f) + K.sum(y_pred_f) + 1.)
    return loss / label_length


def average_dice_coef_loss(y_true, y_pred):
    return -average_dice_coef(y_true, y_pred)


def load_model(input_shape, num_labels, axis=-1, base_filter=32, depth_size=4, se_res_block=True, se_ratio=16,
               noise=0.1, last_relu=True):
    def conv3d(layer_input, filters, axis=-1, se_res_block=True, se_ratio=16, down_sizing=True):
        if down_sizing == True:
            layer_input = MaxPooling3D(pool_size=(2, 2, 2))(layer_input)
        d = Conv3D(filters, (3, 3, 3), use_bias=False, padding='same')(layer_input)
        d = InstanceNormalization(axis=axis)(d)
        d = LeakyReLU(alpha=0.3)(d)
        d = Conv3D(filters, (3, 3, 3), use_bias=False, padding='same')(d)
        d = InstanceNormalization(axis=axis)(d)
        if se_res_block == True:
            se = GlobalAveragePooling3D()(d)
            se = Dense(filters // se_ratio, activation='relu')(se)
            se = Dense(filters, activation='sigmoid')(se)
            se = Reshape([1, 1, 1, filters])(se)
            d = Multiply()([d, se])
            shortcut = Conv3D(filters, (3, 3, 3), use_bias=False, padding='same')(layer_input)
            shortcut = InstanceNormalization(axis=axis)(shortcut)
            d = layers.add([d, shortcut])
        d = LeakyReLU(alpha=0.3)(d)
        return d

    def deconv3d(layer_input, skip_input, filters, axis=-1, se_res_block=True, se_ratio=16):
        u1 = ZeroPadding3D(((0, 1), (0, 1), (0, 1)))(layer_input)
        u1 = Conv3DTranspose(filters, (2, 2, 2), strides=(2, 2, 2), use_bias=False, padding='same')(u1)
        u1 = InstanceNormalization(axis=axis)(u1)
        u1 = LeakyReLU(alpha=0.3)(u1)
        u1 = CropToConcat3D()([u1, skip_input])
        u2 = Conv3D(filters, (3, 3, 3), use_bias=False, padding='same')(u1)
        u2 = InstanceNormalization(axis=axis)(u2)
        u2 = LeakyReLU(alpha=0.3)(u2)
        u2 = Conv3D(filters, (3, 3, 3), use_bias=False, padding='same')(u2)
        u2 = InstanceNormalization(axis=axis)(u2)
        if se_res_block == True:
            se = GlobalAveragePooling3D()(u2)
            se = Dense(filters // se_ratio, activation='relu')(se)
            se = Dense(filters, activation='sigmoid')(se)
            se = Reshape([1, 1, 1, filters])(se)
            u2 = Multiply()([u2, se])
            shortcut = Conv3D(filters, (3, 3, 3), use_bias=False, padding='same')(u1)
            shortcut = InstanceNormalization(axis=axis)(shortcut)
            u2 = layers.add([u2, shortcut])
        u2 = LeakyReLU(alpha=0.3)(u2)
        return u2

    def CropToConcat3D():
        def crop_to_concat_3D(concat_layers, axis=-1):
            bigger_input, smaller_input = concat_layers
            bigger_shape, smaller_shape = tf.shape(bigger_input), \
                                          tf.shape(smaller_input)
            sh, sw, sd = smaller_shape[1], smaller_shape[2], smaller_shape[3]
            bh, bw, bd = bigger_shape[1], bigger_shape[2], bigger_shape[3]
            dh, dw, dd = bh - sh, bw - sw, bd - sd
            cropped_to_smaller_input = bigger_input[:, :-dh,
                                       :-dw,
                                       :-dd, :]
            return K.concatenate([smaller_input, cropped_to_smaller_input], axis=axis)

        return Lambda(crop_to_concat_3D)

    input_img = Input(shape=input_shape)
    d0 = GaussianNoise(noise)(input_img)
    d1 = Conv3D(base_filter, (3, 3, 3), use_bias=False, padding='same')(d0)
    d1 = InstanceNormalization(axis=axis)(d1)
    d1 = LeakyReLU(alpha=0.3)(d1)
    d2 = conv3d(d1, base_filter * 2, se_res_block=se_res_block)
    d3 = conv3d(d2, base_filter * 4, se_res_block=se_res_block)
    d4 = conv3d(d3, base_filter * 8, se_res_block=se_res_block)

    if depth_size == 4:
        d5 = conv3d(d4, base_filter * 16, se_res_block=se_res_block)
        u4 = deconv3d(d5, d4, base_filter * 8, se_res_block=se_res_block)
        u3 = deconv3d(u4, d3, base_filter * 4, se_res_block=se_res_block)
    elif depth_size == 3:
        u3 = deconv3d(d4, d3, base_filter * 4, se_res_block=se_res_block)
    else:
        raise Exception('depth size must be 3 or 4. you put ', depth_size)

    u2 = deconv3d(u3, d2, base_filter * 2, se_res_block=se_res_block)
    u1 = ZeroPadding3D(((0, 1), (0, 1), (0, 1)))(u2)
    u1 = Conv3DTranspose(base_filter, (2, 2, 2), strides=(2, 2, 2), use_bias=False, padding='same')(u1)
    u1 = InstanceNormalization(axis=axis)(u1)
    u1 = LeakyReLU(alpha=0.3)(u1)
    u1 = CropToConcat3D()([u1, d1])
    u1 = Conv3D(base_filter, (3, 3, 3), use_bias=False, padding='same')(u1)
    u1 = InstanceNormalization(axis=axis)(u1)
    u1 = LeakyReLU(alpha=0.3)(u1)
    output_img = Conv3D(num_labels, kernel_size=1, strides=1, padding='same', activation='sigmoid')(u1)
    if last_relu == True:
        output_img = ThresholdedReLU(theta=0.5)(output_img)

    model = Model(inputs=input_img, outputs=output_img)
    return model