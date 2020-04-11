from .truthdiscoverer import TruthDiscoverer
from tensorflow_probability import edward2 as ed
import numpy as np
from .utils import logits_for_uniform, observe
import tensorflow as tf
from spectrum.inference.bbvi import BBVI
from spectrum.inference.utils import compute_trust_and_truth


class simpleLCA_EM:
    """Implement simpleLCA using EM.

    Note: we should inherit from TruthDiscover after refactor it to 
    conform to new paradigmn:
        discoverer = TruthDiscover(claims)
        discoverer.discover()

    This implementation is specific to categorical claims.

    References:
    - Latent Credibility Analysis, Jeff Pasternack and Dan Roth

    Parameters
    ----------
    claims: pd.DataFrame
        a data frame that has columns `[source_id, object_id, value]`.
        We expect `source_id`, and `object_id` to of type `int`. `value` could
        be of type `int` if they are labels for things such as gender,
        diseases. It is of type `float` if it represents things such as sensor
        reading, etc.

    auxiliary_data: dict
        a dictionary that contain auxiliary data, e.g., source features.
        The default value is None.
    """
    def __init__(self, claims, auxiliary_data=None):
        # setup book keeping data structures
        self.claims = claims.copy()
        self.claims['claim_id'] = np.arange(0, len(claims))
        self.observation = self.compute_observation_matrix()
        self.weight = self.compute_weight_matrix()
        self.n_sources, self.n_objects, self.domain_size = self._compute_prob_desc(
        )
        # posterior p(o|X,\theta_old), o is m in the paper.
        self.posterior = dict()
        # prior p(o)
        self.prior = dict()
        for o in range(self.n_objects):
            self.prior[o] = np.ones(
                shape=(self.domain_size[o], )) / self.domain_size[o]

        # building (s_id, o_id) ->v dict
        data = claims[['source_id', 'object_id', 'value']].values
        self.value_index = dict()
        for i in range(data.shape[0]):
            self.value_index[(data[i][0], data[i][1])] = data[i][2]

        # model parameters, source honesty
        self.theta_old = 0.5 * np.ones(shape=(self.n_sources, ))
        self.theta_new = 0.99 * np.ones(shape=(self.n_sources, ))

    def discover(self, alpha=1e-4):
        """Discover true claims and data source reliability

        Parameters
        ----------
        alpha: float
            convergence threshold for the EM algorithm. This is 
            L2 norm of (theta_old - theta_new), where theta is
            the simpleLCA parameters.

        Returns
        -------
        trust: dict
            a dictionary `{source_id, ed.RandomVariable}`.
            Some algorithm-based truth discovery method such as majority voting
            or Truth Finder, does not model source reliability using
            distribution, instead they output a reliablity score. We
            capture this situation using ed.Deterministic(loc=reliablity_score). 
            For other methods, such as LCAs, we use ed.Categorical to model
            reliablities of data sources.

        truth: dict
            a dictionary `{object_id, ed.RandomVariable}` mapping `object_id`
            to an `ed.RandomVariable`. In spectrum, we model the uncertainty
            of truths using probability distribution, which is represented as
            a random variate `ed.RandomVariable`.
        """
        step = 0
        while (True):
            self.e_step()
            self.m_step()
            diff = np.linalg.norm(self.theta_old - self.theta_new, ord=2)
            print(f'difference at step {step}: {diff} - threshold {alpha}')
            if (diff < alpha):
                break
            else:
                self.theta_old = self.theta_new
                step += 1
        return self._compute_trust(), self._compute_truth()

    def e_step(self):
        """compute posterior distribution of truths."""
        for o in range(self.n_objects):
            joint_prob = self.compute_joint(o)
            self.posterior[o] = joint_prob / np.sum(joint_prob)

    def m_step(self):
        """update data source honest."""
        for s in range(self.n_sources):
            self.compute_honest(s)

    def compute_honest(self, s_id):
        number_of_objects = self.weight[s_id].sum()  # could be faster
        honest = 0.
        for o in range(self.n_objects):
            for v in range(self.domain_size[o]):
                mask = self.weight[s_id][o] * (self.get_value(s_id, o) == v)
                honest = honest + self.posterior[o][v] * mask
        self.theta_new[s_id] = honest / number_of_objects

    def compute_joint(self, o_id):
        """compute p(o,B|theta_old)

        In the paper this is p(ym, X|theta_old).

        Parameters
        ----------
        o_id: int
            object id
        Returns
        -------
        p(o,B|theta_old): np.array
            joint distribution of object and data.
        """
        responsibility = np.ones(shape=(self.domain_size[o_id], ))
        for v in range(len(responsibility)):
            responsibility[v] = self.compute_responsibility(o_id, v)
        joint_prob = self.prior[o_id] * responsibility
        return joint_prob

    def compute_responsibility(self, o_id, v):
        """compute responsibility of data sources when making assertion.

        Responsibility of data sources with respect to value v on the domain
        of object o_id is defined as
            r = prod_{s}r_{s,v}

        where
            r_{s,v} = (theta_s**b_{s,v} * \prod_{c!=v}[(1-theta_s)/(|o|-1)]**b_{s,c})**w_so

        Parameters
        ----------
        o_id: int
            object id, i.e., m

        v: int
            a value on the object's domain

        Returns
        -------
        resp: float
            responsiblity of data sources with respect to the value v of
            object o_id.
        """
        resp = np.ones(shape=(self.n_sources, ))
        for s_id in range(self.n_sources):
            if self.weight[s_id][
                    o_id] == 1:  # weight=0 no need for computation
                if self.get_value(s_id, o_id) == v:
                    resp[s_id] = self.theta_old[s_id]
                else:
                    resp[s_id] = (1 - self.theta_old[s_id]) / (
                        self.domain_size[o_id])
        return np.prod(resp)

    def get_value(self, s_id, o_id):
        if (s_id, o_id) in self.value_index:
            return self.value_index[(s_id, o_id)]
        return None

    def _compute_trust(self):
        trust = dict()
        for s in range(self.n_sources):
            trust[s] = ed.Bernoulli(probs=self.theta_old[s])
        return trust

    def _compute_truth(self):
        truth = dict()
        for o in range(self.n_objects):
            truth[o] = ed.Categorical(probs=self.posterior[o])
        return truth

    def compute_weight_matrix(self):
        """compute weight matrix weight = [w_so]that is is used to train

        s: index source. s is source_id
        o: index mutual execlusive set of claims, e.g. "Claimed Birth Years of Barack Obama". m is object_id.

        Returns
        -------
        weight: np.ndarray
            a 2D matrix of shape (S,O) that represents observation.
            S is the number of data sources, O is the number of objects
        """

        W = self.claims[['source_id', 'object_id',
                         'value']].pivot(index='source_id',
                                         columns='object_id',
                                         values='value')
        W.fillna(value=-1, inplace=True)
        W[W >= 0] = 1
        W[W < 0] = 0
        return W.values

    def compute_observation_matrix(self):
        """compute observation matrix B.

        B[source_id, claim_id] = value. When source source_id does not
        make claim claim_id then B[source_idd, claim_id] = -1.

        Returns
        -------
        B: 2d np.array
            observation matrix.
        """
        B = self.claims.pivot(index='source_id',
                              columns='claim_id',
                              values='value')
        B.fillna(-1, inplace=True)
        return B.values

    def _compute_prob_desc(self):
        """compute statistics of a given truth discovery problem.

        Returns
        -------
        n_sources: int
            number of data sources

        n_objects: int
            number of objects

        domain_size: dict
            domain_size[object_id] is the number of unique values of object
            object_id, i.e., |m| in the original paper.
        """
        problem_sizes = self.claims.nunique()
        n_sources = problem_sizes['source_id']
        n_objects = problem_sizes['object_id']
        domain_size = self.claims.groupby('object_id').max()['value'] + 1
        return n_sources, n_objects, domain_size


class simpleLCA_VI:
    """
    Parameters
    ----------
    claims: pd.DataFrame
        a data frame that has columns `[source_id, object_id, value]`. We
        expect `source_id`, and `object_id` to of type `int`. `value`
        could be of type `int` if they are labels for things such as
        gender, diseases. It is of type `float` if it represents things
        such as sensor reading, etc.

    auxiliary_data: dict
        a dictionary of auxliary data of some sort.
    """
    def __init__(self, claims, auxiliary_data=None):
        self.claims = claims.copy()
        self.auxiliary_data = auxiliary_data
        self.build_ds()
        self.init_vars()
        self.observed_model = observe(self.model, self.obs)

    def compute_observation(self):
        """build observation dictionary.

        Returns
        -------
        obs: dict
            mapping observed random variable to their corresponding observation.
        """
        obs = dict()

        def assign(data):
            obs[f'x_{data.name}'] = data.sort_values(
                'source_id')['value'].values

        self.claims.groupby('object_id').apply(lambda x: assign(x))
        return obs

    def build_ds(self):
        """build auxiliary data structure"""
        self.n_sources, self.n_objects, self.domain_size = self._compute_prob_desc(
        )

        self.domsize_to_objects = self.build_domsize_to_objects()
        self.object_to_sources = self.build_object_to_sources()
        self.to_batch_idx = self.compute_batch_idx()
        self.obs = self.compute_observation()

    def build_domsize_to_objects(self):
        """build a dictionary mapping domain size to a list of objects having
        that domain size.
        
        Returns
        -------
        domsize_to_objects: dict
            mapping domsize to a sorted list of object ids
        """
        domain_df = self.claims.groupby('object_id').max()['value'] + 1
        domain_df = domain_df.reset_index().rename(
            columns={'value': 'domain_size'})

        domsize_to_objects = dict()

        def assign(data):
            domsize_to_objects[data.name] = sorted(data['object_id'].values)

        domain_df.groupby('domain_size').apply(lambda data: assign(data))

        for k in domsize_to_objects:
            domsize_to_objects[k] = sorted(domsize_to_objects[k])
        return domsize_to_objects

    def build_object_to_sources(self):
        """build a dictionary mapping object to sources that make claims about it.
        """
        object_to_sources = dict()

        def assign(data):
            object_to_sources[data.name] = sorted(data['source_id'].values)

        self.claims.groupby('object_id').apply(lambda data: assign(data))
        return object_to_sources

    def init_vars(self):
        """initialze model and latent variables."""
        # model vars
        self.model_vars = []
        self.honest_logits_p = tf.Variable(initial_value=tf.zeros(
            self.n_sources, 1),
                                           name='honest_logits_p')
        self._register(self.honest_logits_p, self.model_vars)

        self.object_logits_p = dict()
        for d in self.domsize_to_objects:
            self.object_logits_p[d] = tf.Variable(
                initial_value=logits_for_uniform(
                    len(self.domsize_to_objects[d]), d),
                name=f'truth_logits_p_{d}')
            self._register(self.object_logits_p[d], self.model_vars)

        # latent vars
        self.latent_vars = []
        self.object_logits_q = dict()
        for d in self.domsize_to_objects:
            self.object_logits_q[d] = tf.Variable(
                initial_value=logits_for_uniform(
                    len(self.domsize_to_objects[d]), d),
                name=f'truth_logits_q_{d}')
            self._register(self.object_logits_q[d], self.latent_vars)

    def model(self):
        """Build a simpleLCA generative model

        z_truth_dom_size rv batchs all hidden truth rvs (y_m) whose domain size
        is dom_size

        x_o_id rv batchs all observed rvs of objects o_id.
        """
        # z_truth
        z_truths = dict()
        for d in self.domsize_to_objects:
            z_truths[d] = ed.Categorical(name=f'z_truth_{d}',
                                         logits=tf.math.log_softmax(
                                             self.object_logits_p[d]))

        # x_oid
        for o_id in range(self.n_objects):
            truth_rv = z_truths[self.domain_size[o_id]]
            truth = truth_rv[self.to_batch_idx[o_id]]
            ed.Categorical(name=f'x_{o_id}',
                           probs=self.compute_observed_probs(o_id, truth))

    def mean_field_model(self):
        """a mean field varational model"""
        # q_truth
        for d in self.domsize_to_objects:
            ed.Categorical(name=f'z_truth_{d}',
                           logits=tf.math.log_softmax(self.object_logits_q[d]))

    def discover(self,
                 epochs=1,
                 learning_rate=1e-4,
                 report_every=1,
                 n_samples=1,
                 compute_variance=False,
                 n_gradient_samples=5):
        """Discover true claims and data source reliability

        Parameters
        ----------
        n_samples: int
        the number of samples to be used to estimate gradients of BBVI loss.

        compute_variance: bool
            if compute_variance=False then variance of score-function gradient estimator
            is estimated at each epoch using n_gradient_samples.

        n_gradient_samples: bool
            the number of gradient estimation to be used when compute its variance. It will
            be ignored if compute_variance=False. 

        Returns
        -------
        trust: dict
            a dictionary `{source_id, ed.RandomVariable}`. Some algorithmic
            truth discovery method such as majority voting or Truth Finder,
            does not model source reliability using distribution, instead they
            output a reliablity score. We capture this situation using
            ed.Deterministic(loc=reliablity_score). For other methods, such as
            LCAs, we use ed.Categorical to model reliablities of data sources.

        truth: dict
            a dictionary `{object_id, ed.RandomVariable}` mapping `object_id`
            to an `ed.RandomVariable`. In spectrum, we model the uncertainty
            of truths using probability distribution, which is represented as
            a random variate `ed.RandomVariable`.
        """
        # peform black-box bvi
        self.bbvi = BBVI(p=self.observed_model,
                         q=self.mean_field_model,
                         p_vars=self.model_vars,
                         q_vars=self.latent_vars,
                         n_samples=n_samples,
                         compute_variance=compute_variance,
                         n_gradient_samples=n_gradient_samples)

        self.bbvi.train(epochs=epochs,
                        learning_rate=learning_rate,
                        report_every=report_every)

        # return compute_trust_and_truth(self.mean_field_model)

    def _compute_rank(self, o_id):
        rank = 0
        for o in self.domsize_to_objects[self.domain_size[o_id]]:
            if o_id > o:
                rank += 1
        return rank

    def compute_batch_idx(self):
        """compute batch index dictionary.

        Returns
        -------
        to_batch_idx: dict
            to_batch_idx[o_id] is the batch index of an object into its corresponding batched z_truth
        """
        to_batch_idx = dict()
        for o_id in range(self.n_objects):
            to_batch_idx[o_id] = self._compute_rank(o_id)
        return to_batch_idx

    def compute_observed_probs(self, o_id, truth):
        """compute observation for an object.

        Parameters
        ----------
        o_id: int
            object id.

        truth: int
            the truth value of the object.

        Returns
        -------
        observed_probs: tf.Tensor
            a |S_o_id|*domsize(o_id) tf.Tensor of observed probabilities.
        """
        domsize = self.domain_size[o_id]
        observed_probs = []
        for s in self.object_to_sources[o_id]:
            honest_prob = tf.math.sigmoid(self.honest_logits_p[s])
            observed_probs.append(
                self._build_claim_probs(honest_prob, domsize, truth))
        return tf.stack(observed_probs)

    def _build_claim_probs(self, honest_prob, domain_size, truth):
        mask = tf.reduce_sum(tf.one_hot([truth], domain_size), axis=0)
        other = tf.ones(domain_size) - mask
        probs = mask * honest_prob * tf.ones(domain_size) + other * (
            (1 - honest_prob) / (domain_size - 1)) * tf.ones(domain_size)
        return probs

    def _register(self, variable, collection):
        if isinstance(variable, list):
            collection.extend(variable)
        else:
            collection.append(variable)

    def _compute_prob_desc(self):
        """compute statistics of a given truth discovery problem.

        Returns
        -------
        n_sources: int
            number of data sources

        n_objects: int
            number of objects

        domain_size: dict
            domain_size[object_id] is the number of unique values of object
            object_id, i.e., |m| in the original paper.
        """
        problem_sizes = self.claims.nunique()
        n_sources = problem_sizes['source_id']
        n_objects = problem_sizes['object_id']
        domain_size = self.claims.groupby('object_id').max()['value'] + 1
        return n_sources, n_objects, domain_size