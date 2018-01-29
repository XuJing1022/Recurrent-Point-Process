import numpy as np
import matplotlib, sys
matplotlib.use('agg')
import tensorflow as tf
from BatchIterator import PaddedDataIterator
from generation import *

##############################################################################
# parameters
BATCH_SIZE = 256  # Batch size
MAX_STEPS = 300  # maximum length of your sequence
ITERS = 30000  # how many generator iterations to train for
REG = 0.1  # tradeoff between time and mark loss
LR = 1e-4  # learning rate
TYPE = 'joint'  # sys.argv[1] # model type: joint event timeseries
NUM_steps_timeseries = 7  # timeseries steps before one event
Timeseries_feature = 4  # time series feature size

SEED = 12345  # set graph-level seed to make the random sequences generated by all ops be repeatable across sessions
tf.set_random_seed(SEED)
np.random.seed(SEED)

##############################################################################
# prepare data

# this is just toy data to test the code.
DIM_SIZE = 7  # equal num of classes
mi = MarkedIntensityHomogenuosPoisson(DIM_SIZE)
for u in range(DIM_SIZE):
    mi.initialize(1.0, u)
simulated_sequences = generate_samples_marked(mi, 15.0, 1000)  # 1000个[[],[],...]
event_iterator = PaddedDataIterator(simulated_sequences, 0, MARK=True, DIFF=True)


# time_series_data = np.ones((BATCH_SIZE,real_batch[0].shape[1],NUM_steps_timeseries,4))

###############################################################################
# define model

def RNNPP(rnn_inputs_event,  # dims batch_size x num_steps x input_size(mark&time), pad with 0 if variable length
          rnn_inputs_timeseries,
          # batch_size x num_steps x num_steps_timeseries x input_size, pad with 0 if variable length
          seqlen,  # sequence length for each sequence, tf.int32 vector
          lower_triangular_ones,  # lower triangular matrix
          num_classes=7,  # number of dimensions for event sequence
          loss='mse',  # loss type for time: mse and intensity, intensity loss comes from Du, etc. KDD16
          start=3,  # predict forward event starting at start-th event for each sequence
          reg=REG,  # loss trade-off between mark and time
          state_size_event=16,  # RNN state size for event sequence
          state_size_timeseries=32,  # RNN state size for time series
          batch_size=BATCH_SIZE,
          scope_reuse=False):
    epilson = tf.constant(1e-3, tf.float32)

    with tf.variable_scope("RNNPP") as scope:
        if scope_reuse:
            scope.reuse_variables()

        num_steps = tf.shape(rnn_inputs_event)[1]
        event_size = tf.shape(rnn_inputs_event)[2]
        y = tf.concat([rnn_inputs_event[:, 1:, :], rnn_inputs_event[:, :1, :]], axis=1)
        y = tf.reshape(y, [-1, event_size])  # pass '[-1]' to flatten

        if TYPE == 'joint' or TYPE == 'event':
            with tf.variable_scope("event") as scope:
                # rnn for event sequence 
                rnn_input_onehot = tf.one_hot(tf.cast(rnn_inputs_event[:, :, 0], tf.int32), num_classes)  # when num_class is large, use tf embedding
                rnn_inputs_event = tf.concat([rnn_input_onehot, rnn_inputs_event[:, :, 1:]], axis=2)
                cell = tf.contrib.rnn.BasicRNNCell(state_size_event)  # cell = tf.contrib.rnn.LSTMCell(state_size,state_is_tuple=True) is perferred
                init_state = cell.zero_state(batch_size, tf.float32)
                rnn_outputs_event, final_state = tf.nn.dynamic_rnn(cell, rnn_inputs_event, sequence_length=seqlen, initial_state=init_state)

        if TYPE == 'joint' or TYPE == 'timeseries':
            with tf.variable_scope("timeseries") as scope:
                # rnn for time series
                cell = tf.contrib.rnn.BasicRNNCell(state_size_timeseries)  # cell = tf.contrib.rnn.LSTMCell(state_size,state_is_tuple=True) is perferred
                init_state = cell.zero_state(batch_size * num_steps, tf.float32)
                rnn_inputs_timeseries = tf.reshape(rnn_inputs_timeseries, [-1, NUM_steps_timeseries, Timeseries_feature])
                rnn_outputs_timeseries, final_state = tf.nn.dynamic_rnn(cell, rnn_inputs_timeseries, initial_state=init_state)

                rnn_outputs_timeseries = tf.reshape(rnn_outputs_timeseries[:, -1, :], [batch_size, num_steps, state_size_timeseries])

        # reshape rnn_outputs
        if TYPE == 'joint':
            rnn_outputs = tf.concat([rnn_outputs_event, rnn_outputs_timeseries], axis=2)
        elif TYPE == 'event':
            rnn_outputs = rnn_outputs_event
        elif TYPE == 'timeseries':
            rnn_outputs = rnn_outputs_timeseries

        rnn_outputs_shape = tf.shape(rnn_outputs)
        rnn_outputs = tf.reshape(rnn_outputs, [-1, rnn_outputs_shape[-1]])

        # linear layer
        with tf.variable_scope('time'):
            if TYPE == 'joint':
                W_t = tf.get_variable('Wt', [state_size_event + state_size_timeseries, 1])
            elif TYPE == 'event':
                W_t = tf.get_variable('Wt', [state_size_event, 1])
            elif TYPE == 'timeseries':
                W_t = tf.get_variable('Wt', [state_size_timeseries, 1])

            w_t = tf.get_variable('wt', [1], initializer=tf.constant_initializer(1.0))
            b_t = tf.get_variable('bt', [1], initializer=tf.constant_initializer(0.0))

        if loss == 'intensity':  # 体现点过程之处
            wt = tf.cond(tf.less(tf.reshape(tf.abs(w_t), []), epilson), lambda: tf.sign(wt) * epilson, lambda: wt)  # put in wrong position before
            part1 = tf.matmul(rnn_outputs, W_t) + b_t  # mat multiple
            part2 = wt * y[:, 1]
            time_loglike = part1 + part2 + (tf.exp(part1) - tf.exp(part1 + part2)) / wt
            time_loss = - time_loglike

        elif loss == 'mse':
            time_hat = tf.matmul(rnn_outputs, W_t) + b_t
            time_loss = tf.abs(tf.reshape(time_hat, [-1]) - y[:, 1])

        # Softmax layer
        with tf.variable_scope('softmax'):
            if TYPE == 'joint':
                W_l = tf.get_variable('Wl', [state_size_event + state_size_timeseries, num_classes])
            elif TYPE == 'event':
                W_l = tf.get_variable('Wl', [state_size_event, num_classes])
            elif TYPE == 'timeseries':
                W_l = tf.get_variable('Wl', [state_size_timeseries, num_classes])

            b_l = tf.get_variable('bl', [num_classes], initializer=tf.constant_initializer(0.0))  # ?

        mark_logits = tf.matmul(rnn_outputs, W_l) + b_l
        mark_true = tf.one_hot(tf.cast(y[:, 0], tf.int32), num_classes)
        mark_loss = tf.nn.softmax_cross_entropy_with_logits(logits=mark_logits, labels=mark_true)

        total_loss = mark_loss + reg * time_loss

        # length of y minus 2 to drop last prediction
        seqlen_mask = tf.slice(tf.gather(lower_triangular_ones, seqlen - 2), [0, start], [batch_size, num_steps - start])
        zeros_pad = tf.zeros([batch_size, start])
        seqlen_mask = tf.concat([zeros_pad, seqlen_mask], axis=1)

        mark_loss = tf.reshape(mark_loss, [batch_size, num_steps])
        mark_loss *= seqlen_mask
        # Average over actual sequence lengths.
        mark_loss = tf.reduce_sum(mark_loss, axis=1)
        mark_loss = tf.reduce_mean(mark_loss)

        total_loss = tf.reshape(total_loss, [batch_size, num_steps])
        total_loss *= seqlen_mask  # why 256*256 vs 256*140
        # Average over actual sequence lengths.
        total_loss = tf.reduce_sum(total_loss, axis=1)
        total_loss = tf.reduce_mean(total_loss)

        time_loss = total_loss - mark_loss

    return total_loss, mark_loss, time_loss


event_sequence = tf.placeholder(tf.float32, shape=[BATCH_SIZE, None, 2])  # 列为2，行不定
time_series = tf.placeholder(tf.float32, shape=[BATCH_SIZE, None, NUM_steps_timeseries, Timeseries_feature])

seqlen = tf.placeholder(tf.int32, shape=[BATCH_SIZE])
lower_triangular_ones = tf.constant(np.tril(np.ones([MAX_STEPS, MAX_STEPS])), dtype=tf.float32)

total_loss, mark_loss, time_loss = RNNPP(event_sequence, time_series, seqlen, lower_triangular_ones)

train_variables = tf.trainable_variables()
joint_variables = [v for v in train_variables if v.name.startswith("RNNPP")]
print(map(lambda x: x.op.name, joint_variables))

train_op = tf.train.RMSPropOptimizer(learning_rate=LR).minimize(total_loss, var_list=joint_variables)

##################################################################################
# run

# Add ops to save and restore all the variables.
saver = tf.train.Saver()

gpu_options = tf.GPUOptions(per_process_gpu_memory_fraction=1.0, allow_growth=True)
sess = tf.Session(config=tf.ConfigProto(allow_soft_placement=True, gpu_options=gpu_options))

sess.run(tf.global_variables_initializer())

# train
for it in range(ITERS):
    if TYPE == 'joint':
        real_batch = event_iterator.next_batch(BATCH_SIZE)
        time_series_data = np.ones((BATCH_SIZE, real_batch[0].shape[1], NUM_steps_timeseries, 4))
        total_loss_curr, mark_loss_curr, time_loss_curr, _ = sess.run([total_loss, mark_loss, time_loss, train_op],
                                                                      feed_dict={event_sequence: real_batch[0], seqlen: real_batch[1], time_series: time_series_data})

        print('Iter: {};  Total loss: {:.4};  Mark loss: {:.4};  Time loss: {:.4}'.format(it, total_loss_curr, mark_loss_curr, time_loss_curr))

    if TYPE == 'event':
        real_batch = event_iterator.next_batch(BATCH_SIZE)
        total_loss_curr, mark_loss_curr, time_loss_curr, _ = sess.run([total_loss, mark_loss, time_loss, train_op],
                                                                      feed_dict={event_sequence: real_batch[0], seqlen: real_batch[1]})

        print('Iter: {};  Total loss: {:.4};  Mark loss: {:.4};  Time loss: {:.4}'.format(it, total_loss_curr, mark_loss_curr, time_loss_curr))

    if TYPE == 'timeseries':
        real_batch = event_iterator.next_batch(BATCH_SIZE)
        time_series_data = np.ones((BATCH_SIZE, real_batch[0].shape[1], NUM_steps_timeseries, 4))
        total_loss_curr, mark_loss_curr, time_loss_curr, _ = sess.run([total_loss, mark_loss, time_loss, train_op],
                                                                      feed_dict={event_sequence: real_batch[0], seqlen: real_batch[1], time_series: time_series_data})

        print('Iter: {};  Total loss: {:.4};  Mark loss: {:.4};  Time loss: {:.4}'.format(it, total_loss_curr, mark_loss_curr, time_loss_curr))
