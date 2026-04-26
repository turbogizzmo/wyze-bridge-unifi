"""
Compatibility patch for spyne with Python 3.12+
Fixes the missing spyne.util.six.moves module issue
"""
import sys
from types import ModuleType
from collections.abc import MutableSet, Sequence, Iterable
from io import BytesIO, StringIO
from http.cookies import SimpleCookie
from urllib.parse import unquote, quote


def add_metaclass(metaclass):
    """Class decorator for creating a class with a metaclass.
    Implements the six.add_metaclass functionality"""
    def wrapper(cls):
        orig_vars = cls.__dict__.copy()
        slots = orig_vars.get('__slots__')
        if slots is not None:
            if isinstance(slots, str):
                slots = [slots]
            for slots_var in slots:
                orig_vars.pop(slots_var)
        orig_vars.pop('__dict__', None)
        orig_vars.pop('__weakref__', None)
        return metaclass(cls.__name__, cls.__bases__, orig_vars)
    return wrapper


def with_metaclass(meta, *bases):
    """Create a base class with a metaclass.
    Implements the six.with_metaclass functionality"""
    # This requires a bit of explanation: the basic idea is to make a temporary
    # metaclass (called 'metaclass') that replaces itself with the actual
    # metaclass.
    class metaclass(type):
        def __new__(cls, name, this_bases, d):
            return meta(name, bases, d)

        @classmethod
        def __prepare__(cls, name, this_bases):
            if hasattr(meta, '__prepare__'):
                return meta.__prepare__(name, bases)
            return {}
    return type.__new__(metaclass, 'temporary_class', (), {})


def get_function_name(func):
    """Get the name of a function.
    Implements the six.get_function_name functionality"""
    return func.__name__


# Create mock six module
mock_six = ModuleType('spyne.util.six')
mock_six.PY2 = False
mock_six.PY3 = True
mock_six.text_type = str
mock_six.binary_type = bytes
mock_six.string_types = (str,)
mock_six.add_metaclass = add_metaclass
mock_six.with_metaclass = with_metaclass
mock_six.BytesIO = BytesIO
mock_six.StringIO = StringIO
mock_six.get_function_name = get_function_name

# Create moves submodule
mock_moves = ModuleType('spyne.util.six.moves')
mock_six.moves = mock_moves

# Create collections_abc submodule
mock_collections_abc = ModuleType('spyne.util.six.moves.collections_abc')
mock_collections_abc.MutableSet = MutableSet
mock_collections_abc.Sequence = Sequence
mock_collections_abc.Iterable = Iterable
mock_moves.collections_abc = mock_collections_abc

# Create http_cookies submodule
mock_http_cookies = ModuleType('spyne.util.six.moves.http_cookies')
mock_http_cookies.SimpleCookie = SimpleCookie
mock_moves.http_cookies = mock_http_cookies

# Create urllib submodule
mock_urllib = ModuleType('spyne.util.six.moves.urllib')
mock_moves.urllib = mock_urllib

# Create urllib.parse submodule
mock_urllib_parse = ModuleType('spyne.util.six.moves.urllib.parse')
mock_urllib_parse.unquote = unquote
mock_urllib_parse.quote = quote
mock_urllib.parse = mock_urllib_parse

# Inject the mock modules into sys.modules before spyne imports them
sys.modules['spyne.util.six'] = mock_six
sys.modules['spyne.util.six.moves'] = mock_moves
sys.modules['spyne.util.six.moves.collections_abc'] = mock_collections_abc
sys.modules['spyne.util.six.moves.http_cookies'] = mock_http_cookies
sys.modules['spyne.util.six.moves.urllib'] = mock_urllib
sys.modules['spyne.util.six.moves.urllib.parse'] = mock_urllib_parse