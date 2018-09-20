import os
import time

import numpy as np
import tensorflow as tf
from a_nice_mc.utils.bootstrap import Buffer
from a_nice_mc.utils.logger import create_logger
from a_nice_mc.utils.nice import TrainingOperator, InferenceOperator
#from a_nice_mc.utils.hmc import HamiltonianMonteCarloSampler as HmcSampler
#from hepmc.core.integration.multi_channel import MultiChannel
from hepmc.core.markov.metropolis import DefaultMetropolis
from hepmc.core.phase_space.mapping import MappedDensity
from hepmc.core.phase_space.rambo import RamboOnDiet
from hepmc.core.densities.qcd import ee_qq_ng
#from hepmc.core.densities.gaussian import Gaussian


class Trainer(object):
    """
    Trainer for A-NICE-MC.
    - Wasserstein GAN loss with Gradient Penalty for x
    - Cross entropy loss for v

    Maybe for v we can use MMD loss, but in my experiments
    I didn't see too much of an improvement over cross entropy loss.
    """
    def __init__(self,
                 network, energy_fn, discriminator,
                 noise_sampler,
                 b, m, eta=1.0, scale=10.0):
        self.energy_fn = energy_fn
        self.logger = create_logger(__name__)
        self.train_op = TrainingOperator(network)
        self.infer_op = InferenceOperator(network, energy_fn)
        self.b = tf.to_int32(tf.reshape(tf.multinomial(tf.ones([1, b]), 1), [])) + 1
        self.m = tf.to_int32(tf.reshape(tf.multinomial(tf.ones([1, m]), 1), [])) + 1
        self.network = network
        self.hmc_sampler = None
        self.x_dim, self.v_dim = network.x_dim, network.v_dim


        self.z = tf.placeholder(tf.float32, [None, self.x_dim])
        self.x = tf.placeholder(tf.float32, [None, self.x_dim])
        self.xl = tf.placeholder(tf.float32, [None, self.x_dim])
        self.steps = tf.placeholder(tf.int32, [])
        self.nice_steps = tf.placeholder(tf.int32, [])
        bx, bz = tf.shape(self.x)[0], tf.shape(self.z)[0]

        # Obtain values from inference ops
        # `infer_op` contains Metropolis step
        v = tf.random_normal(tf.stack([bz, self.v_dim]))
        self.z_, self.v_ = self.infer_op((self.z, v), self.steps, self.nice_steps)

        # Reshape for pairwise discriminator
        x = tf.reshape(self.x, [-1, 2 * self.x_dim])
        xl = tf.reshape(self.xl, [-1, 2 * self.x_dim])

        # Obtain values from train ops
        v1 = tf.random_normal(tf.stack([bz, self.v_dim]))
        x1_, v1_ = self.train_op((self.z, v1), self.b)
        x1_ = x1_[-1]
        x1_sg = tf.stop_gradient(x1_)
        v2 = tf.random_normal(tf.stack([bx, self.v_dim]))
        x2_, v2_ = self.train_op((self.x, v2), self.m)
        x2_ = x2_[-1]
        v3 = tf.random_normal(tf.stack([bx, self.v_dim]))
        x3_, v3_ = self.train_op((x1_sg, v3), self.m)
        x3_ = x3_[-1]

        # The pairwise discriminator has two components:
        # (x, x2) from x -> x2
        # (x1, x3) from z -> x1 -> x3
        #
        # The optimal case is achieved when x1, x2, x3
        # are all from the data distribution
        x_ = tf.concat([
                tf.concat([x2_, self.x], 1),
                tf.concat([x3_, x1_], 1)
        ], 0)

        # Concat all v values for log-likelihood training
        v1_ = v1_[-1]
        v2_ = v2_[-1]
        v3_ = v3_[-1]
        v_ = tf.concat([v1_, v2_, v3_], 0)
        v_ = tf.reshape(v_, [-1, self.v_dim])

        d = discriminator(x, reuse=False)
        d_ = discriminator(x_)

        # generator loss

        # TODO: MMD loss (http://szhao.me/2017/06/10/a-tutorial-on-mmd-variational-autoencoders.html)
        # it is easy to implement, but maybe we should wait after this codebase is settled.
        self.v_loss = tf.reduce_mean(0.5 * tf.multiply(v_, v_))
        self.g_loss = tf.reduce_mean(d_) + self.v_loss * eta

        # discriminator loss
        self.d_loss = tf.reduce_mean(d) - tf.reduce_mean(d_)

        epsilon = tf.random_uniform([], 0.0, 1.0)
        x_hat = xl * epsilon + x_ * (1 - epsilon)
        d_hat = discriminator(x_hat)
        ddx = tf.gradients(d_hat, x_hat)[0]
        ddx = tf.norm(ddx, axis=1)
        ddx = tf.reduce_mean(tf.square(ddx - 1.0) * scale)
        self.d_loss = self.d_loss + ddx

        # I don't have a good solution to the tf variable scope mess.
        # So I basically force the NiceLayer to contain the 'generator' scope.
        # See `nice/__init__.py`.
        g_vars = [var for var in tf.global_variables() if 'generator' in var.name]
        d_vars = [var for var in tf.global_variables() if discriminator.name in var.name]

        self.d_train = tf.train.AdamOptimizer(learning_rate=5e-4, beta1=0.5, beta2=0.9)\
            .minimize(self.d_loss, var_list=d_vars)
        self.g_train = tf.train.AdamOptimizer(learning_rate=5e-4, beta1=0.5, beta2=0.9)\
            .minimize(self.g_loss, var_list=g_vars)

        self.init_op = tf.group(
            tf.global_variables_initializer(),
            tf.local_variables_initializer()
        )

        gpu_options = tf.GPUOptions(allow_growth=True)
        self.sess = tf.Session(config=tf.ConfigProto(
            #inter_op_parallelism_threads=1,
            #intra_op_parallelism_threads=1,
            inter_op_parallelism_threads=0,
            intra_op_parallelism_threads=0,
            gpu_options=gpu_options,
        ))

        self.sess.run(self.init_op)
        self.ns = noise_sampler
        self.ds = None
        self.path = 'logs/' + energy_fn.name
        try:
            os.makedirs(self.path)
        except OSError:
            pass



    def sample(self, steps=2000, nice_steps=1, batch_size=32):
        start = time.time()
        z, v = self.sess.run([self.z_, self.v_], feed_dict={
            self.z: self.ns(batch_size), self.steps: steps, self.nice_steps: nice_steps})
        end = time.time()
        self.logger.info('A-NICE-MC: batches [%d] steps [%d : %d] time [%5.4f] samples/s [%5.4f]' %
                         (batch_size, steps, nice_steps, end - start, (batch_size * steps) / (end - start)))
        z = np.transpose(z, axes=[1, 0, 2])
        v = np.transpose(v, axes=[1, 0, 2])
        return z, v

    def bootstrap(self, steps=5000, nice_steps=1, burn_in=1000, batch_size=32,
                  discard_ratio=0.5, use_hmc=False):
        # TODO: it might be better to implement bootstrap in a separate class
        if use_hmc:

            if not self.hmc_sampler:
                #self.hmc_sampler = HmcSampler(self.energy_fn,
                #                              lambda bs: np.random.randn(bs, self.x_dim),
                #                              sess=self.sess)
                #channels = [Gaussian(self.x_dim, mu=1/3, cov=.1**2/2), Gaussian(self.x_dim, mu=2/3, cov=.1**2/2)]
                #self.hmc_sampler = MultiChannel(channels)
                target = ee_qq_ng(2, 100., 5., .3)
                rambo_mapping = RamboOnDiet(100., 4)
                mapped = MappedDensity(target, rambo_mapping)
                self.hmc_sampler = DefaultMetropolis(mapped, cov=.01)

            print('before hmc_sampler.sample')
            #z = self.hmc_sampler.sample(steps, batch_size)
            #z = self.hmc_sampler.rvs(steps*batch_size)
            start = self.hmc_sampler.sample(1000, np.random.rand(8)).data[-1]
            z = self.hmc_sampler.sample(steps*batch_size, start).data
            z = np.reshape(z, (batch_size, steps, self.x_dim))
            print('steps:', steps)
            print('batch_size:', batch_size)
            print('type(z):', type(z))
            print('z.shape:', z.shape)
            print('z.min:', z.min())
            print('z.max:', z.max())
            print('after hmc_sampler.sample')
        else:
            z, _ = self.sample(steps + burn_in, nice_steps, batch_size)
        z = np.reshape(z[:, burn_in:], [-1, z.shape[-1]])
        if self.ds:
            self.ds.discard(ratio=discard_ratio)
            self.ds.insert(z)
        else:
            self.ds = Buffer(z)

    def train(self,
              d_iters=5, epoch_size=500, log_freq=100, max_iters=100000,
              bootstrap_steps=5000, bootstrap_burn_in=1000,
              bootstrap_batch_size=32, bootstrap_discard_ratio=0.5,
              evaluate_steps=5000, evaluate_burn_in=1000, evaluate_batch_size=32, nice_steps=1,
              hmc_epochs=1):
        """
        Train the NICE proposal using adversarial training.
        :param d_iters: number of discrtiminator iterations for each generator iteration
        :param epoch_size: how many iteration for each bootstrap step
        :param log_freq: how many iterations for each log on screen
        :param max_iters: max number of iterations for training
        :param bootstrap_steps: how many steps for each bootstrap
        :param bootstrap_burn_in: how many burn in steps for each bootstrap
        :param bootstrap_batch_size: # of chains for each bootstrap
        :param bootstrap_discard_ratio: ratio for discarding previous samples
        :param evaluate_steps: how many steps to evaluate performance
        :param evaluate_burn_in: how many burn in steps to evaluate performance
        :param evaluate_batch_size: # of chains for evaluating performance
        :param nice_steps: Experimental.
            num of steps for running the nice proposal before MH. For now do not use larger than 1.
        :param hmc_epochs: number of epochs to bootstrap off HMC rather than NICE proposal
        :return:
        """
        def _feed_dict(bs):
            return {self.z: self.ns(bs), self.x: self.ds(bs), self.xl: self.ds(4 * bs)}

        batch_size = 32
        train_time = 0
        num_epochs = 0
        use_hmc = True
        for t in range(0, max_iters):
            if t % epoch_size == 0:
                num_epochs += 1
                if num_epochs > hmc_epochs:
                    use_hmc = False
                print('before bootstrap')
                self.bootstrap(
                    steps=bootstrap_steps, burn_in=bootstrap_burn_in,
                    batch_size=bootstrap_batch_size, discard_ratio=bootstrap_discard_ratio,
                    use_hmc=use_hmc
                )
                print('before sample')
                z, v = self.sample(evaluate_steps + evaluate_burn_in, nice_steps, evaluate_batch_size)
                print('z:', z)
                print('v:', v)
                z, v = z[:, evaluate_burn_in:], v[:, evaluate_burn_in:]
                self.energy_fn.evaluate([z, v], path=self.path)
                # TODO: save model
            if t % log_freq == 0:
                d_loss = self.sess.run(self.d_loss, feed_dict=_feed_dict(batch_size))
                g_loss, v_loss = self.sess.run([self.g_loss, self.v_loss], feed_dict=_feed_dict(batch_size))
                self.logger.info('Iter [%d] time [%5.4f] d_loss [%.4f] g_loss [%.4f] v_loss [%.4f]' %
                                 (t, train_time, d_loss, g_loss, v_loss))
            start = time.time()
            for _ in range(0, d_iters):
                self.sess.run(self.d_train, feed_dict=_feed_dict(batch_size))
            self.sess.run(self.g_train, feed_dict=_feed_dict(batch_size))
            end = time.time()
            train_time += end - start

    def load(self):
        # TODO: load model
        raise NotImplementedError(str(type(self)))

    def save(self):
        # TODO: save model
        raise NotImplementedError(str(type(self)))
