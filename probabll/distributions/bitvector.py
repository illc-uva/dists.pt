import torch
import torch.distributions as td
from torch.autograd import Function
from torch.autograd.function import once_differentiable
from torch.distributions import constraints
from torch.distributions.exp_family import ExponentialFamily
from itertools import product
import numpy as np


class NonEmptyBitVector(td.Distribution):
    """
    This class manipulates distributions over bit-vectors with the constraint that the outcome 'all zeros' is not in the sample space.
    """

    has_rsample = False
    has_enumerate_support = True

    @classmethod
    def _arc_weight_and_state_value(cls, scores):
        """
        For a batch of size B, we build an FSA to sample from a distribution
        over {0,1}^K \setminus {0^K}

        Each distribution assigns probability 
        p(x|scores) \propto \prod_k \exp(x_k * scores_k - (1-x_k) * scores_k)

        We need to normalise the distribution over valid assignments of x, this 
        affects the pmf and thus also sampling. For that we build an FSA with 
        (K+1)*3 states. Each state is identified by an index k 
        (a position in a K-dimensional bit vectore)
        and a label L (out of 3 possible labels):

        * a state (k, L=0) means that the kth bit is 0 and sum(x[:k+1])=0
        * a state (k, L=1) means that the kth bit is 0 and thus sum(x[:k+1])>0
        * a state (k, L=2) means that the kth bit is 1 and thus sum(x[:k+1])>0

        The initial state of the FSA is (k=0, L=0) 
        and it connects to (k=1,L=0) or (k=1, L=2), 
        each arc weighted with the corresponding log potential.
        The final state is (k=K+1,L=1).

        :param scores: log pontetials with shape [B, K]
        :return:
            * FSA structure represented as a boolean tensor with shape [B, K+1, 3, 3]
            * weight of outgoing arcs with shape [B, K+1, 3, 3]
            * (backward/inside) value of states with shape [B, K+2, 3]
        """
        batch_shape, K = scores.shape[:-1], scores.shape[-1]
        # initialise weights of outgoing arcs from initial state (k=0, L=*) 
        #  and every intermediate state until the pre-final states (k=K, L=*)
        # the initial value is semiring.zero (-inf for LogProb semiring)
        # also initialise a boolean tensor indicating valid arcs 
        M = torch.zeros(batch_shape + (K+1, 3, 3), device=scores.device, dtype=torch.bool)
        W = torch.zeros(batch_shape + (K+1, 3, 3), device=scores.device) - np.inf
        # we have 3 possible labels
        # W[b, k, 0]: x_{b,k+1} is False, sum(x_{b,:k+1}) + x_{b,k+1} = 0
        # W[b, k, 1]: x_{b,k+1} is False, sum(x_{b,:k+1}) + x_{b,k+1} > 0
        # W[b, k, 2]: x_{b,k+1} is True, sum(x_{b,:k+1}) + x_{b,k+1} > 0

        # Our scores are log potentials (and semiring.zero = -inf)
        ninf = torch.zeros_like(scores) - np.inf

        # For states corresponding to k <= K we have

        # arcs from (k,L=0) to (k+1,L=0) and (k+1,L=2) 
        W[...,:-1,0,:] = torch.stack([-scores, ninf, scores], -1)
        M[...,:-1,0,0] = True
        M[...,:-1,0,2] = True

        # arcs from (k,L=1) to (k+1,L=1) and (k+1,L=2) 
        W[...,2:-1,1,:] = torch.stack([ninf[...,2:], -scores[...,2:], scores[...,2:]], -1)
        M[...,2:-1,1,1] = True
        M[...,2:-1,1,2] = True

        # arcs from (k,L=2) to (k+1,L=1) and (k+1,L=2)
        W[...,1:-1,2,:] = torch.stack([ninf[...,1:], -scores[...,1:], scores[...,1:]], -1)
        M[...,1:-1,2,1] = True
        M[...,1:-1,2,2] = True

        # arcs from pre-final states to final state
        W[...,K,1,1] = 0
        W[...,K,2,1] = 0
        M[...,K,1,1] = True
        M[...,K,2,1] = True

        # Compute values and reverse values: https://www.aclweb.org/anthology/J99-4004.pdf
        # This can be a bit confusing, so let me make some connections.
        # The value recursion is known as the backward algorithm for HMMs
        #  or inside algorithm for PCFGs.
        # The reverse value recursion is known as the forward algorithm for HMMs
        #  (which is just the backward ran the other way around)
        #  or inside algorithm for PCFGs (unlike in the case of forward/backward, 
        #  inside is not just outside in a "reversed" graph).
        # I am using the language of value/reverse value simply because of familiarity
        #  and because these concepts are less confusing to me.

        # Initialise the value of every state with semiring.zero
        V = torch.zeros(batch_shape + (K+2, 3), device=scores.device) - np.inf
        # the only final state (k=K+1, L=1), has weight semiring.one
        V[...,K+1,1] = 0
        # The value of the initial state will give us the log normaliser of the distribution.

        # Initialise the reverse value of every state with semiring.zero
        R = torch.zeros(batch_shape + (K+2, 3), device=scores.device) - np.inf
        # the only initial state (k=0,L=0) has weight semiring.one
        R[...,0,0] = 0

        # Compute values and reverse values in a single linear pass
        for k in torch.flip(torch.arange(K+1, device=scores.device), [-1]): 
            # [...,3,3]
            m_k = M[...,k,:,:]
            w_k = W[...,k,:,:]
            V[...,k,:] = torch.logsumexp(torch.where(m_k, w_k + V[...,k+1,None,:], m_k.float() - np.inf), dim=-1)
            # [...,3,3]
            m_rk = M[...,K-k,:,:]
            w_rk = W[...,K-k,:,:]
            R[...,K-k+1,:] = torch.logsumexp(torch.where(m_rk, w_rk + R[...,K-k,:,None], m_rk.float() - np.inf), dim=-2)        

        return M, W, V, R
    
    def __init__(self, scores, validate_args=False):
        """
        :param scores: [B, K]
        """
        self._scores = scores  # [B, K]
        batch_shape, K = scores.shape[:-1], scores.shape[-1]
        event_shape = torch.Size([K])
        super().__init__(batch_shape, event_shape, validate_args)
        self._K = K
        self._fsa, self._arc_weight, self._state_value, self._state_rvalue = self._arc_weight_and_state_value(scores)

    def expand(self, batch_shape, _instance=None):
        new = self._get_checked_instance(NonEmptyBitVector, _instance)
        batch_shape = torch.Size(batch_shape)
        new._scores = self._scores.expand(batch_shape + self.event_shape)        
        new._fsa = self._fsa.expand(batch_shape + (self._K+1, 3, 3))
        new._arc_weight = self._arc_weight.expand(batch_shape + (self._K+1, 3, 3))
        new._state_value = self._state_value.expand(batch_shape + (self._K+2, 3))
        new._state_rvalue = self._state_rvalue.expand(batch_shape + (self._K+2, 3))
        new._K = self._K
        super(NonEmptyBitVector, new).__init__(batch_shape, self.event_shape, validate_args=False)
        new._validate_args = self._validate_args
        return new

    @property
    def dim(self):
        return self._K 

    @property
    def support_size(self):
        return 2 ** self._K - 1

    @property
    def fsa(self):
        return self._fsa

    @property
    def scores(self):
        return self._scores

    @property
    def arc_weight(self):
        return self._arc_weight

    @property
    def state_value(self):
        return self._state_value

    @property
    def state_rvalue(self):
        return self._state_rvalue
    
    def log_prob(self, value):        
        log_prob = torch.where(value.bool(), self._scores, -self._scores).sum(-1)
        return log_prob - self._state_value[...,0,0]

    def sample(self, sample_shape=torch.Size()):    

        # In comments, I use S as an indication of dimension(s) related to sample_shape
        # and B as an indication of dimension(s) related to batch_shape
        with torch.no_grad():
            batch_shape, K = self.batch_shape, self._K  
            # This will store the sequence of labels from k=0 to K+1
            # [S, B, K+2]
            L = torch.zeros(sample_shape + batch_shape + (K+2,), device=self._scores.device).long()            
            # [L, B, K+2, 3]
            eps = td.Gumbel(
                    loc=torch.zeros(L.shape + (3,), device=self._scores.device), 
                    scale=torch.ones(L.shape + (3,), device=self._scores.device)
            ).sample()
            # [...,K+1,3,3]
            W = self._arc_weight
            # [...,K+2,3]
            V = self._state_value
            for k in torch.arange(K+1, device=self._scores.device): 
                # weights of arcs leaving this coordinate
                # [B, 3, 3]
                W_k = W[...,k,:,:]
                # reshape to introduce sample_shape dimensions
                # [S, B, 3, 3]
                W_k = W_k.view((1,) * len(sample_shape) + W_k.shape).expand(sample_shape + (-1,)*len(W_k.shape))
                # origin state for coordinate k
                # [S, B]
                L_k = L[...,k]
                # reshape to a 3-dimensional one-hot encoding of the label 
                # [S, B, 3, 1]
                L_k = torch.nn.functional.one_hot(L_k, 3).unsqueeze(-1)
                # select the weights for destination (zeroing out the rest)
                # [S, B, 3, 3]
                logits_k = torch.where(L_k == 1, W_k, torch.zeros_like(W_k))
                # sum 0s out and incorporate value of destination
                # [S, B, 3]
                logits_k = logits_k.sum(-2) + V[...,k+1,:] 

                # Categorical sampling via Gumbel-Argmax
                #  possibly more efficient than td.Categorical(logits=logits_k).sample().long()
                L[...,k+1] = torch.argmax(logits_k + eps[...,k+1,:], -1).long()                

            assert (L[...,-1] == 1).all(), "Not every sample reached the final state"
            L = L[...,1:-1]  # discard the initial (k=0) and final (k=K+1) states
            # map to boolean and then float (in torch discrete samples are float)
            return (L==2).float()

    def enumerate_support(self, expand=True, max_K=10):
        """
        Return a bit-vector representation of all non-empty faces of 
        K-1 dimensional simplex. 

        The return has dtype=float (rather than bool) because in torch 
        outcomes are always float. The shape is [2^K-1, B, K]
        """
        if max_K:
            assert self._K <= max_K, f"You probably do not want to enumerate the {self.support_size} outcomes in the sample space?"
        # [support_size, K]
        faces = torch.tensor(
            [x for x in product([0, 1], repeat=self._K) if sum(x)], 
            device=self._scores.device).float()    
        if expand:
            num_faces, K = faces.shape
            # [num_faces, ..., K]
            faces = faces.view((num_faces,) + (1,)*len(self.batch_shape) + (K,))
            # [num_faces, B, K]
            faces = faces.expand((num_faces,)  + self.batch_shape + (K,))            
        return faces 

    def cross_entropy(self, other):
        """
        Compute H(p, q) = - \sum_x p(x) \log q(x)
         where p = self, and q = other.

        :return: [B]        
        """
        assert self.scores.shape == other.scores.shape, "The shape of the scores must match"
        
        # We have two global models:
        #   p(x) = (\prod_{e in x} exp(score_p(e))) / Z_p        
        # where Z_p is the normalisation constant
        #   Z_p = \sum_x \prod_{e in x} exp(score_p(e))        
        # and similarly
        #   q(x) = (\prod_{e in x} exp(score_q(e))) / Z_q
        #   Z_q = \sum_x \prod_{e in x} exp(score_q(e))        
        # thus 
        #   log p(x) = (\sum_{e in x} score_p(e)) - log Z_p
        #   log q(x) = (\sum_{e in x} score_q(e)) - log Z_q

        # The cross-entropy is
        #  E[-log q(x)] = -\sum_x p(x) log q(x)
        #   = \sum_x p(x) (log Z_q - \sum_{e in x} score_q(e))
        #   = \sum_x p(x) log Z_q - \sum_x p(x) \sum_{e in x} score_q(e) 
        #   = log Z_q - \sum_x p(x) \sum_{e in x} score_q(e)
        #   = log Z_q - \sum_{e} mu(e) score_q(e)
        # where mu(e) is the marginal probability of the edge under the distribution p_X
        #  mu(e) = R[ori(e)] * exp(score_p(e)) * V[dest(e)]  / Z_p
        #  R[ori(e)] is the total probability of all paths from the initial state
        #  of the FSA to the origin state of the edge
        #  V[dest(e)] is the total probability of all paths from the final state 
        #  of the FSA to the destination state of the edge.

        # A fully vectorised implementation is possible, but it's tricky to 
        #  visualise. I'll try my best to explain it here.
        M = self.fsa
        W = self.arc_weight
        V = self.state_value
        R = self.state_rvalue
        # Recall that
        #  * M.shape is [B, K+1, 3, 3] (this is a boolean mask indicating the valid arcs)
        #  * W.shape is [B, K+1, 3, 3]
        #  * V.shape is [B, K+2, 3]        
        #  and the batch dimension B could actually be a tuple of dimensions.
        # I want to vectorise the following computation
        #  R[...,k,ori] + W[...,k,ori,dest] + V[...,k+1,dest]) - V[...,0,0]
        #  for some k in {0,...,K}, ori in {0,1,2} and dest in {0,1,2}, here
        #  ori is the label of the origin state, and dest is the label of the 
        #  destination state.
        # So I am going to change R's shape, 
        #  it goes from [B, K+2, 3] to [B, K+1, 3, 1],
        #  where I forget k=K+1 and unsqueeze the last dimension.
        # Similarly, V goes from [B, K+2, 3] to [B, K+1, 1, 3],
        #  where I forget k=0 and unsqueeze the second last dimension.
        # The result has the same shape of W, which is desirable
        #  since we are computing expected values for the edge potentials
        #  (note we should mask this operation following the FSA structure M, 
        #   that's because we are subtracting quantities that are potentially -inf, 
        #   which would lead to NaNs)
        # [B, K+1, 3, 3]
        log_mu = torch.where(M, R[...,:-1,:,None] + W + V[...,1:,None,:] - V[...,0,0,None,None,None], M.float() - np.inf)
        # We need the marginal (not its log)
        mu = log_mu.exp()
        # Here we use masked product, this is needed because we want the semantics
        # inf * 0 = 0, but for good reasons that's not what torch produces 
        # in this specific context, a masked product is wanted and safe.
        expected = torch.where(mu == 0, torch.zeros_like(mu), other.arc_weight * mu)
        # For the entropy simply compute the expected potential and shift by log Z_q
        H = -(expected.sum((-1, -2, -3)) - other.state_value[...,0,0])
        
        # less vectorised code (easier to read)
        #nH = 0.
        #for k in range(K+1):
        #    for ori in range(3):            
        #        for dest in range(3):
        #            # marginal probability of edge
        #            log_w = R[...,k,ori] + W[...,k,ori,dest] + V[...,k+1,dest] - V[...,0,0]
        #            w = log_w.exp()                                        
        #            # expected score
        #            e = other.arc_weight[...,k,ori,dest] * w
        #            e = torch.where(w == 0, torch.zeros_like(e), e)
        #            nH = nH + e
        #H = - (nH - other.state_value[...,0,0])

        return H

    def entropy(self):
        return self.cross_entropy(self)

    def mode(self):
        """
        Return the outcome x whose p_X(x) is maximum. 
        This implementation always returns a single outcome (even if there are ties).
        """
        with torch.no_grad():
            batch_shape, K = self.batch_shape, self._K
            # This will store the sequence of labels from k=0 to K+1
            S = torch.zeros(batch_shape + (K+2,), device=self._scores.device).long()
            for k in torch.arange(K+1, device=self._scores.device): 
                logits_k = self._arc_weight[...,k,S[...,k],:] + self._state_value[...,k+1,:]
                # Categorical sampling via Gumbel-Argmax
                S[...,k+1] = torch.argmax(logits_k, -1).long()

            assert (S[...,-1] == 1).all(), "Not every sample reached the final state"
            S = S[...,1:-1]  # discard the initial (k=0) and final (k=K+1) states
            # map to boolean and then float (in torch discrete samples are float)
            return (S == 2).float()

@td.register_kl(NonEmptyBitVector, NonEmptyBitVector)
def _kl_nonemptybitvector_nonemptybitvector(p, q):
    if p.scores.shape != q.scores.shape: 
        raise ValueError("The shapes of p and q differ")
    return p.cross_entropy(q) - p.entropy()


### Test stuff


from collections import Counter 

def test_non_empty_bit_vector(batch_shape=tuple(), K=3):
    assert K<= 10, "I test against explicit enumeration, K>10 might be too slow for that"
    
    # Uniform 
    F = NonEmptyBitVector(torch.zeros(batch_shape + (K,)))
    
    # Shapes
    assert F.batch_shape == batch_shape, "NonEmptyBitVector has the wrong batch_shape"
    assert F.dim == K, "NonEmptyBitVector has the wrong dim"
    assert F.event_shape == (K,), "NonEmptyBitVector has the wrong event_shape"
    assert F.scores.shape == batch_shape + (K,), "NonEmptyBitVector.score has the wrong shape"
    assert F.arc_weight.shape == batch_shape + (K+1,3,3), "NonEmptyBitVector.arc_weight has the wrong shape"
    assert F.state_value.shape == batch_shape + (K+2,3), "NonEmptyBitVector.state_value has the wrong shape"
    assert F.state_rvalue.shape == batch_shape + (K+2,3), "NonEmptyBitVector.state_rvalue has the wrong shape"
    # shape: [num_faces] + batch_shape + [K]
    support = F.enumerate_support()    
    # test shape of support
    assert support.shape == (2**K-1,) + batch_shape + (K,), "The support has the wrong shape"

    assert F.expand((2,3) + batch_shape).batch_shape == (2,3) + batch_shape, "Bad expand batch_shape"
    assert F.expand((2,3) + batch_shape).event_shape == (K,), "Bad expand event_shape"
    assert F.expand((2,3) + batch_shape).sample().shape == (2,3) + batch_shape + (K,), "Bad expand single sample"
    assert F.expand((2,3) + batch_shape).sample((13,)).shape == (13,2,3) + batch_shape + (K,), "Bad expand multiple samples"

    # Constraints
    assert (support.sum(-1) > 0).all(), "The support has an empty bit vector"
    for _ in range(100):  # testing one sample at a time
        assert F.sample().sum(-1).all(), "I found an empty vector"
    # testing a batch of samples
    assert F.sample((100,)).sum(-1).all(), "I found an empty vector"
    # testing a complex batch of samples
    assert F.sample((2, 100,)).sum(-1).all(), "I found an empty vector"
    
    # Distribution
    # check for uniform probabilities
    assert torch.isclose(F.log_prob(support).exp(), torch.tensor(1./F.support_size)).all(), "Non-uniform"
    # check for uniform marginal probabilities
    assert torch.isclose(F.sample((10000,)).float().mean(0), support.mean(0), atol=1e-1).all(), "Bad marginals"    

    # Entropy

    # [num_faces, B]
    log_prob = F.log_prob(support)
    assert torch.isclose(F.entropy(), (-(log_prob.exp() * log_prob).sum(0)), atol=1e-2).all(), "Problem in the entropy DP"

    # Non-Uniform  

    # Entropy  
    P = NonEmptyBitVector(td.Normal(torch.zeros(batch_shape + (K,)), torch.ones(batch_shape + (K,))).sample())
    log_p = P.log_prob(support)
    assert torch.isclose(P.entropy(), (-(log_p.exp() * log_p).sum(0)), atol=1e-2).all(), "Problem in the entropy DP"
    # Cross-Entropy
    Q = NonEmptyBitVector(td.Normal(torch.zeros(batch_shape + (K,)), torch.ones(batch_shape + (K,))).sample())
    log_q = Q.log_prob(support)
    assert torch.isclose(P.cross_entropy(Q), -(log_p.exp() * log_q).sum(0), atol=1e-2).all(), "Problem in the cross-entropy DP"
    # KL
    assert torch.isclose(td.kl_divergence(P, Q), (log_p.exp() * (log_p - log_q)).sum(0), atol=1e-2).all(), "Problem in KL"

    # Constraints
    for _ in range(100):  # testing one sample at a time
        assert P.sample().sum(-1).all(), "I found an empty vector"
        assert Q.sample().sum(-1).all(), "I found an empty vector"
    # testing a batch of samples
    assert P.sample((100,)).sum(-1).all(), "I found an empty vector"
    assert Q.sample((100,)).sum(-1).all(), "I found an empty vector"
    # testing a complex batch of samples
    assert P.sample((2, 100,)).sum(-1).all(), "I found an empty vector"
    assert Q.sample((2, 100,)).sum(-1).all(), "I found an empty vector"

test_non_empty_bit_vector(K=3)
test_non_empty_bit_vector((3,), K=3)
test_non_empty_bit_vector(K=10)
test_non_empty_bit_vector((5,), K=10)

