import abc

from models import BasicInputData


class InputData(BasicInputData):
    # same as BasicInputData
    pass


class Model(metaclass=abc.ABCMeta):
    @property
    @abc.abstractmethod
    def r(self):
        pass
    
    @property
    @abc.abstractmethod
    def p(self):
        pass
    
    @property
    @abc.abstractmethod
    def mu(self):
        pass
