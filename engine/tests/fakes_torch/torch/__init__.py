"""Minimal torch stand-in for machines without PyTorch (only what the tests touch)."""
import numpy as _np

float32 = _np.float32


class _Tensor:
    def __init__(self, a):
        self.a = _np.asarray(a, dtype=_np.float32)

    def detach(self):
        return self

    def float(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.a

    def __truediv__(self, x):
        return _Tensor(self.a / x)

    def __rmul__(self, x):
        return _Tensor(self.a * x)

    __mul__ = __rmul__


def arange(n, dtype=None):
    return _Tensor(_np.arange(n))


def sin(x):
    return _Tensor(_np.sin(x.a))


def stack(xs):
    return _Tensor(_np.stack([x.a for x in xs]))


class _Cuda:
    @staticmethod
    def is_available():
        return False


cuda = _Cuda()


class version:
    hip = None
    cuda = None


__version__ = "0.0-stub"


class backends:
    mps = None
