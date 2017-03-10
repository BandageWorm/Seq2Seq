import random

import numpy as np
import tensorflow as tf

import data_util

class S2SModel(object):
    def __init__(self,
                source_vocab_size, # size of the source vocabulary.
                target_vocab_size, # size of the target vocabulary.
                buckets, # maximum input/output length pairs (I, O).
                size, # number of units in each layer of the model.
                dropout, # dropout wrapper.
                num_layers, # number of layers in the model.
                max_gradient_norm, # gradients clipped to maximally this norm.
                batch_size, # the size of the batches used during training.
                learning_rate, # learning rate to start with.
                num_samples, # number of samples for sampled softmax.
                forward_only=False, # True = do not construct the backward pass.
                dtype=tf.float32):
        # init member variales
        self.source_vocab_size = source_vocab_size
        self.target_vocab_size = target_vocab_size
        self.buckets = buckets
        self.batch_size = batch_size
        self.learning_rate = learning_rate

        # LSTM cells
        cell = tf.contrib.rnn.BasicLSTMCell(size)
        cell = tf.contrib.rnn.DropoutWrapper(cell, output_keep_prob=dropout)
        cell = tf.contrib.rnn.MultiRNNCell([cell] * num_layers)

        output_projection = None # Softmax Projection
        softmax_loss_function = None

        # If we use sampled softmax, we need an output projection.
        if num_samples > 0 and num_samples < self.target_vocab_size:
            print('Output projection:{}'.format(num_samples))
            w_t = tf.get_variable( "proj_w", [self.target_vocab_size, size],
                dtype=dtype)
            w = tf.transpose(w_t)
            b = tf.get_variable("proj_b", [self.target_vocab_size],
                dtype=dtype)
            output_projection = (w, b)

            def sampled_loss(labels, inputs):
                labels = tf.reshape(labels, [-1, 1])
                local_w_t = tf.cast(w_t, tf.float32)
                local_b = tf.cast(b, tf.float32)
                local_inputs = tf.cast(inputs, tf.float32)
                return tf.cast(
                    tf.nn.sampled_softmax_loss(
                        weights=local_w_t,
                        biases=local_b,
                        labels=labels,
                        inputs=local_inputs,
                        num_sampled=num_samples,
                        num_classes=self.target_vocab_size
                    ),
                    dtype)
            softmax_loss_function = sampled_loss

        # seq2seq_f
        def seq2seq_f(encoder_inputs, decoder_inputs, do_decode):
            return tf.contrib.legacy_seq2seq.embedding_attention_seq2seq(
                encoder_inputs,
                decoder_inputs,
                cell,
                num_encoder_symbols=source_vocab_size,
                num_decoder_symbols=target_vocab_size,
                embedding_size=size,
                output_projection=output_projection,
                feed_previous=do_decode,
                dtype=dtype)

        # inputs
        self.encoder_inputs = []
        self.decoder_inputs = []
        self.decoder_weights = []

        for i in range(buckets[-1][0]):# Last bucket is the biggest one.
            self.encoder_inputs.append(tf.placeholder(
                tf.int32, shape=[None],
                name='encoder_input_{}'.format(i)))
        for i in range(buckets[-1][1] + 1):
            self.decoder_inputs.append(tf.placeholder(
                tf.int32, shape=[None],
                name='decoder_input_{}'.format(i)))
            self.decoder_weights.append(tf.placeholder(
                dtype, shape=[None],
                name='decoder_weight_{}'.format(i)))
        # Our targets are decoder inputs shifted by one.
        targets = [
            self.decoder_inputs[i + 1] for i in range(buckets[-1][1])]

        if forward_only:
            self.outputs, self.losses = tf.contrib.legacy_seq2seq.model_with_buckets(
                self.encoder_inputs, self.decoder_inputs, targets,
                self.decoder_weights, buckets,
                lambda x, y: seq2seq_f(x, y, True),
                softmax_loss_function=softmax_loss_function)
            # If we use output projection, we need to project outputs for decoding.
            if output_projection is not None:
                for b in range(len(buckets)):
                    self.outputs[b] = [
                        tf.matmul(output, output_projection[0]) + output_projection[1]
                        for output in self.outputs[b]]
        else:
            self.outputs, self.losses = tf.contrib.legacy_seq2seq.model_with_buckets(
                self.encoder_inputs, self.decoder_inputs, targets,
                self.decoder_weights, buckets,
                lambda x, y: seq2seq_f(x, y, False),
                softmax_loss_function=softmax_loss_function)

        params = tf.trainable_variables()
        opt = tf.train.AdamOptimizer(learning_rate=learning_rate)

        if not forward_only:
            self.gradient_norms = []
            self.updates = []
            for output, loss in zip(self.outputs, self.losses):
                gradients = tf.gradients(loss, params)
                clipped_gradients, norm = tf.clip_by_global_norm(
                    gradients, max_gradient_norm)
                self.gradient_norms.append(norm)
                self.updates.append(opt.apply_gradients(
                    zip(clipped_gradients, params)))
        # self.saver = tf.train.Saver(tf.all_variables())
        self.saver = tf.train.Saver(tf.global_variables(),
            write_version=tf.train.SaverDef.V2)

    def step(
        self,
        session, # tensorflow session to use.
        encoder_inputs, # list of numpy int vectors to feed as encoder inputs.
        decoder_inputs, # list of numpy int vectors to feed as decoder inputs.
        decoder_weights, # list of numpy float vectors to feed as target weights.
        bucket_id, # which bucket of the model to use.
        forward_only # whether to do the backward step or only forward.
        ):
        encoder_size, decoder_size = self.buckets[bucket_id]
        if len(encoder_inputs) != encoder_size:
            raise ValueError("Encoder length must be equal to the one in bucket,"
                " %d != %d." % (len(encoder_inputs), encoder_size))
        if len(decoder_inputs) != decoder_size:
            raise ValueError("Decoder length must be equal to the one in bucket,"
                " %d != %d." % (len(decoder_inputs), decoder_size))
        if len(decoder_weights) != decoder_size:
            raise ValueError("Weights length must be equal to the one in bucket,"
                " %d != %d." % (len(decoder_weights), decoder_size))

        input_feed = {}
        for i in range(encoder_size):
            input_feed[self.encoder_inputs[i].name] = encoder_inputs[i]
        for i in range(decoder_size):
            input_feed[self.decoder_inputs[i].name] = decoder_inputs[i]
            input_feed[self.decoder_weights[i].name] = decoder_weights[i]

        last_target = self.decoder_inputs[decoder_size].name
        input_feed[last_target] = np.zeros([self.batch_size], dtype=np.int32)

        if not forward_only:
            output_feed = [
                self.updates[bucket_id],
                self.gradient_norms[bucket_id],
                self.losses[bucket_id]]
            # output_feed.append(self.outputs[bucket_id][i])
        else:
            output_feed = [self.losses[bucket_id]]
            for i in range(decoder_size):
                output_feed.append(self.outputs[bucket_id][i])

        outputs = session.run(output_feed, input_feed)
        if not forward_only:
            return outputs[1], outputs[2], outputs[3:]
        else:
            return None, outputs[0], outputs[1:]

    def get_batch_data(self, bucket_dbs, bucket_id):
        data = []
        data_in = []
        bucket_db = bucket_dbs[bucket_id]
        for _ in range(self.batch_size):
            ask, answer = bucket_db.random()
            data.append((ask, answer))
            data_in.append((answer, ask))
        return data, data_in

    def get_batch(self, bucket_dbs, bucket_id, data):
        encoder_size, decoder_size = self.buckets[bucket_id]
        # bucket_db = bucket_dbs[bucket_id]
        encoder_inputs, decoder_inputs = [], []
        for encoder_input, decoder_input in data:
            # encoder_input, decoder_input = random.choice(data[bucket_id])
            # encoder_input, decoder_input = bucket_db.random()
            encoder_input = data_util.sentence_indice(encoder_input)
            decoder_input = data_util.sentence_indice(decoder_input)
            # Encoder
            encoder_pad = [data_util.PAD_ID] * (encoder_size - len(encoder_input))
            encoder_inputs.append(list(reversed(encoder_input + encoder_pad)))
            # Decoder
            decoder_pad_size = decoder_size - len(decoder_input) - 2
            decoder_inputs.append(
                [data_util.GO_ID] + decoder_input +
                [data_util.EOS_ID] +
                [data_util.PAD_ID] * decoder_pad_size)
        batch_encoder_inputs, batch_decoder_inputs, batch_weights = [], [], []
        # batch encoder
        for i in range(encoder_size):
            batch_encoder_inputs.append(np.array(
                [encoder_inputs[j][i] for j in range(self.batch_size)],
                dtype=np.int32))
        # batch decoder
        for i in range(decoder_size):
            batch_decoder_inputs.append(np.array(
                [decoder_inputs[j][i] for j in range(self.batch_size)],
                dtype=np.int32))
            batch_weight = np.ones(self.batch_size, dtype=np.float32)
            for j in range(self.batch_size):
                if i < decoder_size - 1:
                    target = decoder_inputs[j][i + 1]
                if i == decoder_size - 1 or target == data_util.PAD_ID:
                    batch_weight[j] = 0.0
            batch_weights.append(batch_weight)
        return batch_encoder_inputs, batch_decoder_inputs, batch_weights
