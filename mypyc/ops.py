"""Representation of low-level opcodes for compiler intermediate representation (IR).

Opcodes operate on abstract registers in a register machine. Each
register has a type and a name, specified in an environment. A register
can hold various things:

- local variables
- intermediate values of expressions
- condition flags (true/false)
- literals (integer literals, True, False, etc.)
"""

from abc import abstractmethod, abstractproperty
import re
from typing import (
    List, Sequence, Dict, Generic, TypeVar, Optional, Any, NamedTuple, Tuple, Callable,
    Union, Iterable, Type,
)
from collections import OrderedDict

from mypy.nodes import Block, SymbolNode, Var, FuncDef

from mypyc.namegen import NameGenerator
from mypyc.common import TOP_LEVEL_NAME


T = TypeVar('T')


def short_name(name: str) -> str:
    if name.startswith('builtins.'):
        return name[9:]
    return name


class RType:
    """Abstract base class for runtime types (erased, only concrete; no generics)."""

    name = None  # type: str
    is_unboxed = False
    c_undefined = None  # type: str
    is_refcounted = True  # If unboxed: does the unboxed version use reference counting?
    _ctype = None  # type: str  # C type; use Emitter.ctype() to access

    @abstractmethod
    def accept(self, visitor: 'RTypeVisitor[T]') -> T:
        raise NotImplementedError

    def short_name(self) -> str:
        return short_name(self.name)

    def __str__(self) -> str:
        return short_name(self.name)

    def __repr__(self) -> str:
        return '<%s>' % self.__class__.__name__

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RType) and other.name == self.name

    def __hash__(self) -> int:
        return hash(self.name)


class RVoid(RType):
    """void"""

    is_unboxed = False
    name = 'void'
    ctype = 'void'

    def accept(self, visitor: 'RTypeVisitor[T]') -> T:
        return visitor.visit_rvoid(self)


void_rtype = RVoid()


class RPrimitive(RType):
    """Primitive type such as 'object' or 'int'.

    These often have custom ops associated with them.
    """

    def __init__(self,
                 name: str,
                 is_unboxed: bool,
                 is_refcounted: bool,
                 ctype: str = 'PyObject *') -> None:
        self.name = name
        self.is_unboxed = is_unboxed
        self._ctype = ctype
        self.is_refcounted = is_refcounted
        if ctype == 'CPyTagged':
            self.c_undefined = 'CPY_INT_TAG'
        elif ctype == 'PyObject *':
            self.c_undefined = 'NULL'
        elif ctype == 'char':
            self.c_undefined = '2'
        else:
            assert False, 'Uncognized ctype: %r' % ctype

    def accept(self, visitor: 'RTypeVisitor[T]') -> T:
        return visitor.visit_rprimitive(self)

    def __repr__(self) -> str:
        return '<RPrimitive %s>' % self.name


# Used to represent arbitrary objects and dynamically typed values
object_rprimitive = RPrimitive('builtins.object', is_unboxed=False, is_refcounted=True)

int_rprimitive = RPrimitive('builtins.int', is_unboxed=True, is_refcounted=True, ctype='CPyTagged')

float_rprimitive = RPrimitive('builtins.float', is_unboxed=False, is_refcounted=True)

bool_rprimitive = RPrimitive('builtins.bool', is_unboxed=True, is_refcounted=False, ctype='char')

none_rprimitive = RPrimitive('builtins.None', is_unboxed=False, is_refcounted=True)

list_rprimitive = RPrimitive('builtins.list', is_unboxed=False, is_refcounted=True)

dict_rprimitive = RPrimitive('builtins.dict', is_unboxed=False, is_refcounted=True)

set_rprimitive = RPrimitive('builtins.set', is_unboxed=False, is_refcounted=True)

# At the C layer, str is refered to as unicode (PyUnicode)
str_rprimitive = RPrimitive('builtins.str', is_unboxed=False, is_refcounted=True)

# Tuple of an arbitrary length (corresponds to Tuple[t, ...], with explicit '...')
tuple_rprimitive = RPrimitive('builtins.tuple', is_unboxed=False, is_refcounted=True)


def is_int_rprimitive(rtype: RType) -> bool:
    return isinstance(rtype, RPrimitive) and rtype.name == 'builtins.int'


def is_float_rprimitive(rtype: RType) -> bool:
    return isinstance(rtype, RPrimitive) and rtype.name == 'builtins.float'


def is_bool_rprimitive(rtype: RType) -> bool:
    return isinstance(rtype, RPrimitive) and rtype.name == 'builtins.bool'


def is_object_rprimitive(rtype: RType) -> bool:
    return isinstance(rtype, RPrimitive) and rtype.name == 'builtins.object'


def is_none_rprimitive(rtype: RType) -> bool:
    return isinstance(rtype, RPrimitive) and rtype.name == 'builtins.None'


def is_list_rprimitive(rtype: RType) -> bool:
    return isinstance(rtype, RPrimitive) and rtype.name == 'builtins.list'


def is_dict_rprimitive(rtype: RType) -> bool:
    return isinstance(rtype, RPrimitive) and rtype.name == 'builtins.dict'


def is_set_rprimitive(rtype: RType) -> bool:
    return isinstance(rtype, RPrimitive) and rtype.name == 'builtins.set'


def is_str_rprimitive(rtype: RType) -> bool:
    return isinstance(rtype, RPrimitive) and rtype.name == 'builtins.str'


def is_tuple_rprimitive(rtype: RType) -> bool:
    return isinstance(rtype, RPrimitive) and rtype.name == 'builtins.tuple'


class RTuple(RType):
    """Fixed-length unboxed tuple (represented as a C struct)."""

    is_unboxed = True

    def __init__(self, types: List[RType]) -> None:
        self.name = 'tuple'
        self.types = tuple(types)
        # Emitter has logic for generating a C type for RTuple.
        self._ctype = ''
        self.is_refcounted = any(t.is_refcounted for t in self.types)

    def accept(self, visitor: 'RTypeVisitor[T]') -> T:
        return visitor.visit_rtuple(self)

    def __str__(self) -> str:
        return 'tuple[%s]' % ', '.join(str(typ) for typ in self.types)

    def __repr__(self) -> str:
        return '<RTuple %s>' % ', '.join(repr(typ) for typ in self.types)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RTuple) and self.types == other.types

    def __hash__(self) -> int:
        return hash((self.name, self.types))


exc_rtuple = RTuple([object_rprimitive, object_rprimitive, object_rprimitive])


class RInstance(RType):
    """Instance of user-defined class (compiled to C extension class)."""

    is_unboxed = False

    def __init__(self, class_ir: 'ClassIR') -> None:
        self.name = class_ir.name
        self.class_ir = class_ir
        self._ctype = 'PyObject *'

    def accept(self, visitor: 'RTypeVisitor[T]') -> T:
        return visitor.visit_rinstance(self)

    def struct_name(self, names: NameGenerator) -> str:
        return self.class_ir.struct_name(names)

    def getter_index(self, name: str) -> int:
        return self.class_ir.vtable_entry(name)

    def setter_index(self, name: str) -> int:
        return self.getter_index(name) + 1

    def method_index(self, name: str) -> int:
        return self.class_ir.vtable_entry(name)

    def attr_type(self, name: str) -> RType:
        return self.class_ir.attr_type(name)

    def __repr__(self) -> str:
        return '<RInstance %s>' % self.name


class ROptional(RType):
    """Optional[x]"""

    is_unboxed = False

    def __init__(self, value_type: RType) -> None:
        self.name = 'optional'
        self.value_type = value_type
        self._ctype = 'PyObject *'

    def accept(self, visitor: 'RTypeVisitor[T]') -> T:
        return visitor.visit_roptional(self)

    def __repr__(self) -> str:
        return '<ROptional %s>' % self.value_type

    def __str__(self) -> str:
        return 'optional[%s]' % self.value_type

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ROptional) and other.value_type == self.value_type

    def __hash__(self) -> int:
        return hash(('optional', self.value_type))


class AssignmentTarget(object):
    type = None  # type: RType

    @abstractmethod
    def to_str(self, env: 'Environment') -> str:
        raise NotImplementedError


class AssignmentTargetRegister(AssignmentTarget):
    """Register as assignment target"""

    def __init__(self, register: 'Register') -> None:
        self.register = register
        self.type = register.type

    def to_str(self, env: 'Environment') -> str:
        return self.register.name


class AssignmentTargetIndex(AssignmentTarget):
    """base[index] as assignment target"""

    def __init__(self, base: 'Value', index: 'Value') -> None:
        self.base = base
        self.index = index
        # TODO: This won't be right for user-defined classes. Store the
        #       lvalue type in mypy and remove this special case.
        self.type = object_rprimitive

    def to_str(self, env: 'Environment') -> str:
        return '{}[{}]'.format(self.base.name, self.index.name)


class AssignmentTargetAttr(AssignmentTarget):
    """obj.attr as assignment target"""

    def __init__(self, obj: 'Value', attr: str) -> None:
        self.obj = obj
        self.attr = attr
        if isinstance(obj.type, RInstance):
            self.obj_type = obj.type  # type: RType
            self.type = obj.type.attr_type(attr)
        else:
            self.obj_type = object_rprimitive
            self.type = object_rprimitive

    def to_str(self, env: 'Environment') -> str:
        return '{}.{}'.format(self.obj.to_str(env), self.attr)


class AssignmentTargetTuple(AssignmentTarget):
    """x, ..., y as assignment target"""

    def __init__(self, items: List[AssignmentTarget]) -> None:
        self.items = items
        # The shouldn't be relevant, but provide it just in case.
        self.type = object_rprimitive

    def to_str(self, env: 'Environment') -> str:
        return '({})'.format(', '.join(item.to_str(env) for item in self.items))


class Environment:
    """Maintain the register symbol table and manage temp generation"""

    def __init__(self, name: Optional[str] = None) -> None:
        self.name = name
        self.indexes = OrderedDict()  # type: Dict[Value, int]
        self.symtable = {}  # type: Dict[SymbolNode, AssignmentTarget]
        self.temp_index = 0
        self.names = {}  # type: Dict[str, int]

    def regs(self) -> Iterable['Value']:
        return self.indexes.keys()

    def add(self, reg: 'Value', name: str) -> None:
        # Ensure uniqueness of variable names in this environment.
        # This is needed for things like list comprehensions, which are their own scope--
        # if we don't do this and two comprehensions use the same variable, we'd try to
        # declare that variable twice.
        unique_name = name
        while unique_name in self.names:
            unique_name = name + str(self.names[name])
            self.names[name] += 1
        self.names[unique_name] = 0
        reg.name = unique_name

        self.indexes[reg] = len(self.indexes)

    def add_local(self, symbol: SymbolNode, typ: RType, is_arg: bool = False) -> 'Register':
        assert isinstance(symbol, SymbolNode)
        reg = Register(typ, symbol.line, is_arg = is_arg)
        self.symtable[symbol] = AssignmentTargetRegister(reg)
        self.add(reg, symbol.name())
        return reg

    def add_local_reg(self, symbol: SymbolNode,
                      typ: RType, is_arg: bool = False) -> AssignmentTargetRegister:
        self.add_local(symbol, typ, is_arg)
        target = self.symtable[symbol]
        assert isinstance(target, AssignmentTargetRegister)
        return target

    def add_target(self, symbol: SymbolNode, target: AssignmentTarget) -> AssignmentTarget:
        self.symtable[symbol] = target
        return target

    def lookup(self, symbol: SymbolNode) -> AssignmentTarget:
        return self.symtable[symbol]

    def add_temp(self, typ: RType, is_arg: bool = False) -> 'Register':
        assert isinstance(typ, RType)
        reg = Register(typ, is_arg=is_arg)
        self.add(reg, 'r%d' % self.temp_index)
        self.temp_index += 1
        return reg

    def add_op(self, reg: 'RegisterOp') -> None:
        if reg.is_void:
            return
        self.add(reg, 'r%d' % self.temp_index)
        self.temp_index += 1

    def format(self, fmt: str, *args: Any) -> str:
        result = []
        i = 0
        arglist = list(args)
        while i < len(fmt):
            n = fmt.find('%', i)
            if n < 0:
                n = len(fmt)
            result.append(fmt[i:n])
            if n < len(fmt):
                typespec = fmt[n + 1]
                arg = arglist.pop(0)
                if typespec == 'r':
                    result.append(arg.name)
                elif typespec == 'd':
                    result.append('%d' % arg)
                elif typespec == 'f':
                    result.append('%f' % arg)
                elif typespec == 'l':
                    if isinstance(arg, BasicBlock):
                        arg = arg.label
                    result.append('L%s' % arg)
                elif typespec == 's':
                    result.append(str(arg))
                else:
                    raise ValueError('Invalid format sequence %{}'.format(typespec))
                i = n + 2
            else:
                i = n
        return ''.join(result)

    def to_lines(self) -> List[str]:
        result = []
        i = 0
        regs = list(self.regs())

        while i < len(regs):
            i0 = i
            group = [regs[i0].name]
            while i + 1 < len(regs) and regs[i + 1].type == regs[i0].type:
                i += 1
                group.append(regs[i].name)
            i += 1
            result.append('%s :: %s' % (', '.join(group), regs[i0].type))
        return result


class BasicBlock:
    """Basic IR block.

    Ends with a jump, branch, or return.

    When building the IR, ops that raise exceptions can be included in
    the middle of a basic block, but the exceptions aren't checked.
    Afterwards we perform a transform that inserts explicit checks for
    all error conditions and splits basic blocks accordingly to preserve
    the invariant that a jump, branch or return can only ever appear
    as the final op in a block. Manually inserting error checking ops
    would be boring and error-prone.

    BasicBlocks have an error_handler attribute that determines where
    to jump if an error occurs. If none is specified, an error will
    propagate up out of the function. This is compiled away by the
    `exceptions` module.

    Block labels are used for pretty printing and emitting C code, and get
    filled in by those passes.

    Ops that may terminate the program aren't treated as exits.
    """

    def __init__(self, label: int = -1) -> None:
        self.label = label
        self.ops = []  # type: List[Op]
        self.error_handler = None  # type: Optional[BasicBlock]


ERR_NEVER = 0  # Never generates an exception
ERR_MAGIC = 1  # Generates magic value (c_error_value) based on target RType on exception
ERR_FALSE = 2  # Generates false (bool) on exception

# Hack: using this line number for an op will supress it in tracebacks
NO_TRACEBACK_LINE_NO = -10000


class Value:
    # Source line number
    line = -1
    name = '?'
    type = void_rtype  # type: RType
    is_borrowed = False

    def __init__(self, line: int) -> None:
        self.line = line

    @property
    def is_void(self) -> bool:
        return isinstance(self.type, RVoid)

    @abstractmethod
    def to_str(self, env: Environment) -> str:
        raise NotImplementedError


class Register(Value):
    def __init__(self, type: RType, line: int = -1, is_arg: bool = False, name: str = '') -> None:
        super().__init__(line)
        self.name = name
        self.type = type
        self.is_arg = is_arg
        self.is_borrowed = is_arg

    def to_str(self, env: Environment) -> str:
        return self.name

    @property
    def is_void(self) -> bool:
        return False


# An invalid register value.
#
# This is mostly a relic from when the statement and expression
# visitors both returned the same type, but there are some places that
# use it to avoid needing to make some value Optional. Those are
# probably worth cleaning up.
INVALID_VALUE = Register(void_rtype, name='<INVALID_VALUE>')


class Op(Value):
    def __init__(self, line: int) -> None:
        super().__init__(line)

    def can_raise(self) -> bool:
        # Override this is if Op may raise an exception. Note that currently the fact that
        # only RegisterOps may raise an exception in hard coded in some places.
        return False

    @abstractmethod
    def accept(self, visitor: 'OpVisitor[T]') -> T:
        pass

    @abstractmethod
    def sources(self) -> List[Value]:
        pass

    def safe_to_optimize_out(self) -> List[Value]:
        '''
        Whether we consider it safe to remove this operation in a dead code elimination
        or common subexpression elimination pass.

        Any side-effect-free op is safe to DCE/CSE, but some ops (such as casts or boxes/unboxes)
        whose only side effects are type checks are considered safe to eliminate as well.
        '''
        # By default assume DCE is unsafe; only ops marked as safe should be DCEd.
        return False

class ControlOp(Op):
    # Basically just for hierarchy organization.
    # We could plausibly have a targets() method if we wanted.
    pass


class Goto(ControlOp):
    """Unconditional jump."""

    error_kind = ERR_NEVER

    def __init__(self, label: BasicBlock, line: int = -1) -> None:
        super().__init__(line)
        self.label = label

    def __repr__(self) -> str:
        return '<Goto %s>' % self.label.label

    def to_str(self, env: Environment) -> str:
        return env.format('goto %l', self.label)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_goto(self)

    def sources(self) -> List[Value]:
        return []


class Branch(ControlOp):
    """if [not] r1 goto 1 else goto 2"""

    # Branch ops must *not* raise an exception. If a comparison, for example, can raise an
    # exception, it needs to split into two opcodes and only the first one may fail.
    error_kind = ERR_NEVER

    BOOL_EXPR = 100
    IS_ERROR = 101  # Check for magic c_error_value (works for arbitary types)

    op_names = {
        BOOL_EXPR: ('%r', 'bool'),
        IS_ERROR: ('is_error(%r)', ''),
    }

    def __init__(self, left: Value, true_label: BasicBlock,
                 false_label: BasicBlock, op: int, line: int = -1) -> None:
        super().__init__(line)
        self.left = left
        self.true = true_label
        self.false = false_label
        self.op = op
        self.negated = False
        # If not None, the true label should generate a traceback entry (func name, line number)
        self.traceback_entry = None  # type: Optional[Tuple[str, int]]

    def sources(self) -> List[Value]:
        return [self.left]

    def to_str(self, env: Environment) -> str:
        fmt, typ = self.op_names[self.op]
        if self.negated:
            fmt = 'not {}'.format(fmt)

        cond = env.format(fmt, self.left)
        tb = ''
        if self.traceback_entry:
            tb = ' (error at %s:%d)' % self.traceback_entry
        fmt = 'if {} goto %l{} else goto %l'.format(cond, tb)
        if typ:
            fmt += ' :: {}'.format(typ)
        return env.format(fmt, self.true, self.false)

    def invert(self) -> None:
        self.negated = not self.negated

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_branch(self)

class Return(ControlOp):
    error_kind = ERR_NEVER

    def __init__(self, reg: Value, line: int = -1) -> None:
        super().__init__(line)
        self.reg = reg

    def to_str(self, env: Environment) -> str:
        return env.format('return %r', self.reg)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_return(self)

    def sources(self) -> List[Value]:
        return [self.reg]


class Unreachable(ControlOp):
    """Added to the end of non-None returning functions.

    Mypy statically guarantees that the end of the function is not unreachable
    if there is not a return statement.

    This prevents the block formatter from being confused due to lack of a leave
    and also leaves a nifty note in the IR. It is not generally processed by visitors.
    """

    error_kind = ERR_NEVER

    def __init__(self, line: int = -1) -> None:
        super().__init__(line)

    def to_str(self, env: Environment) -> str:
        return "unreachable"

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_unreachable(self)

    def sources(self) -> List[Value]:
        return []

class RegisterOp(Op):
    """An operation that can be written as r1 = f(r2, ..., rn).

    Takes some registers, performs an operation and generates an output.
    The output register can be None for no output.
    """

    error_kind = -1  # Can this raise exception and how is it signalled; one of ERR_*

    _type = None  # type: Optional[RType]

    def __init__(self, line: int) -> None:
        super().__init__(line)
        assert self.error_kind != -1, 'error_kind not defined'

    def can_raise(self) -> bool:
        return self.error_kind != ERR_NEVER

    def unique_sources(self) -> List[Value]:
        result = []  # type: List[Value]
        for reg in self.sources():
            if reg not in result:
                result.append(reg)
        return result


class IncRef(RegisterOp):
    """inc_ref r"""

    error_kind = ERR_NEVER

    def __init__(self, src: Value, line: int = -1) -> None:
        assert src.type.is_refcounted
        super().__init__(line)
        self.src = src

    def to_str(self, env: Environment) -> str:
        s = env.format('inc_ref %r', self.src)
        if is_bool_rprimitive(self.src.type) or is_int_rprimitive(self.src.type):
            s += ' :: {}'.format(short_name(self.src.type.name))
        return s

    def sources(self) -> List[Value]:
        return [self.src]

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_inc_ref(self)


class DecRef(RegisterOp):
    """dec_ref r"""

    error_kind = ERR_NEVER

    def __init__(self, src: Value, line: int = -1) -> None:
        assert src.type.is_refcounted
        super().__init__(line)
        self.src = src

    def __repr__(self) -> str:
        return '<DecRef %r>' % self.src

    def to_str(self, env: Environment) -> str:
        s = env.format('dec_ref %r', self.src)
        if is_bool_rprimitive(self.src.type) or is_int_rprimitive(self.src.type):
            s += ' :: {}'.format(short_name(self.src.type.name))
        return s

    def sources(self) -> List[Value]:
        return [self.src]

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_dec_ref(self)


class Call(RegisterOp):
    """Native call f(arg, ...)

    The call target can be a module-level function or a class.
    """

    error_kind = ERR_MAGIC

    # TODO: take a FuncIR and extract the ret type
    def __init__(self, ret_type: RType, fn: str, args: Sequence[Value], line: int) -> None:
        super().__init__(line)
        self.fn = fn
        self.args = list(args)
        self.type = ret_type

    def to_str(self, env: Environment) -> str:
        args = ', '.join(env.format('%r', arg) for arg in self.args)
        # TODO: Display long name?
        short_name = self.fn.rpartition('.')[2]
        s = '%s(%s)' % (short_name, args)
        if not self.is_void:
            s = env.format('%r = ', self) + s
        return s

    def sources(self) -> List[Value]:
        return list(self.args[:])

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_call(self)


class MethodCall(RegisterOp):
    """Native method call obj.m(arg, ...) """

    error_kind = ERR_MAGIC

    # TODO: extract the ret type from the receiver
    def __init__(self,
                 ret_type: RType,
                 obj: Value,
                 method: str,
                 args: List[Value],
                 line: int = -1) -> None:
        super().__init__(line)
        self.obj = obj
        self.method = method
        self.args = args
        assert isinstance(obj.type, RInstance), "Methods can only be called on instances"
        self.receiver_type = obj.type
        self.type = ret_type

    def to_str(self, env: Environment) -> str:
        args = ', '.join(env.format('%r', arg) for arg in self.args)
        s = env.format('%r.%s(%s)', self.obj, self.method, args)
        if not self.is_void:
            s = env.format('%r = ', self) + s
        return s

    def sources(self) -> List[Value]:
        return self.args[:] + [self.obj]

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_method_call(self)


class EmitterInterface:
    @abstractmethod
    def reg(self, name: Value) -> str:
        raise NotImplementedError

    @abstractmethod
    def c_error_value(self, rtype: RType) -> str:
        raise NotImplementedError

    @abstractmethod
    def temp_name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def emit_line(self, line: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def emit_lines(self, *line: str) -> None:
        raise NotImplementedError

    @abstractmethod
    def emit_declaration(self, line: str) -> None:
        raise NotImplementedError


EmitCallback = Callable[[EmitterInterface, List[str], str], None]

OpDescription = NamedTuple(
    'OpDescription', [('name', str),
                      ('arg_types', List[RType]),
                      ('result_type', Optional[RType]),
                      ('is_var_arg', bool),
                      ('error_kind', int),
                      ('format_str', str),
                      ('emit', EmitCallback),
                      ('priority', int)])  # To resolve ambiguities, highest priority wins


class PrimitiveOp(RegisterOp):
    """reg = op(reg, ...)

    These are register-based primitive operations that work on specific
    operand types.

    The details of the operation are defined by the 'desc'
    attribute. The mypyc.ops_* modules define the supported
    operations. mypyc.genops uses the descriptions to look for suitable
    primitive ops.
    """

    def __init__(self,
                 args: List[Value],
                 desc: OpDescription,
                 line: int) -> None:
        if not desc.is_var_arg:
            assert len(args) == len(desc.arg_types)
        self.error_kind = desc.error_kind
        super().__init__(line)
        self.args = args
        self.desc = desc
        if desc.result_type is None:
            assert desc.error_kind == ERR_FALSE  # TODO: No-value ops not supported yet
            self.type = bool_rprimitive
        else:
            self.type = desc.result_type

    def sources(self) -> List[Value]:
        return list(self.args)

    def __repr__(self) -> str:
        return '<PrimitiveOp name=%r args=%s>' % (self.desc.name,
                                                  self.args)

    def to_str(self, env: Environment) -> str:
        params = {}  # type: Dict[str, Any]
        if not self.is_void:
            params['dest'] = env.format('%r', self)
        args = [env.format('%r', arg) for arg in self.args]
        params['args'] = args
        params['comma_args'] = ', '.join(args)
        return self.desc.format_str.format(**params)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_primitive_op(self)


class Assign(Op):
    """dest = int"""

    error_kind = ERR_NEVER

    def __init__(self, dest: Register, src: Value, line: int = -1) -> None:
        super().__init__(line)
        self.src = src
        self.dest = dest

    def sources(self) -> List[Value]:
        return [self.src]

    def to_str(self, env: Environment) -> str:
        return env.format('%r = %r', self.dest, self.src)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_assign(self)

    def safe_to_optimize_out(self) -> bool:
        return True

class LoadInt(RegisterOp):
    """dest = int"""

    error_kind = ERR_NEVER

    def __init__(self, value: int, line: int = -1) -> None:
        super().__init__(line)
        self.value = value
        self.type = int_rprimitive

    def sources(self) -> List[Value]:
        return []

    def to_str(self, env: Environment) -> str:
        return env.format('%r = %d', self, self.value)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_load_int(self)

    def safe_to_optimize_out(self) -> bool:
        return True

class LoadErrorValue(RegisterOp):
    """dest = <error value for type>"""

    error_kind = ERR_NEVER

    def __init__(self, rtype: RType, line: int = -1) -> None:
        super().__init__(line)
        self.type = rtype

    def sources(self) -> List[Value]:
        return []

    def to_str(self, env: Environment) -> str:
        return env.format('%r = <error> :: %s', self, self.type)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_load_error_value(self)


class GetAttr(RegisterOp):
    """dest = obj.attr (for a native object)"""

    error_kind = ERR_MAGIC

    def __init__(self, obj: Value, attr: str, line: int) -> None:
        super().__init__(line)
        self.obj = obj
        self.attr = attr
        assert isinstance(obj.type, RInstance), 'Attribute access not supported: %s' % obj.type
        self.class_type = obj.type
        self.type = obj.type.attr_type(attr)

    def sources(self) -> List[Value]:
        return [self.obj]

    def to_str(self, env: Environment) -> str:
        return env.format('%r = %r.%s', self, self.obj, self.attr)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_get_attr(self)


class SetAttr(RegisterOp):
    """obj.attr = src (for a native object)"""

    error_kind = ERR_FALSE

    def __init__(self, obj: Value, attr: str, src: Value, line: int) -> None:
        super().__init__(line)
        self.obj = obj
        self.attr = attr
        self.src = src
        assert isinstance(obj.type, RInstance), 'Attribute access not supported: %s' % obj.type
        self.class_type = obj.type
        self.type = bool_rprimitive

    def sources(self) -> List[Value]:
        return [self.obj, self.src]

    def to_str(self, env: Environment) -> str:
        return env.format('%r.%s = %r; %r = is_error', self.obj, self.attr, self.src, self)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_set_attr(self)


# Default name space for statics, variables
NAMESPACE_STATIC = 'static'
# Static namespace for pointers to native type objects
NAMESPACE_TYPE = 'type'


class LoadStatic(RegisterOp):
    """dest = name :: static

    Load a C static variable/pointer. The namespace for statics is shared
    for the entire compilation unit. You can optionally provide a module
    name and a sub-namespace identifier for additional namespacing to avoid
    name conflicts. The static namespace does not overlap with other C names,
    since the final C name will get a prefix, so conflicts only must be
    avoided with other statics.
    """

    error_kind = ERR_NEVER
    is_borrowed = True

    def __init__(self,
                 type: RType,
                 identifier: str,
                 module_name: Optional[str] = None,
                 namespace: str = NAMESPACE_STATIC,
                 line: int = -1,
                 ann: object = None) -> None:
        super().__init__(line)
        self.identifier = identifier
        self.module_name = module_name
        self.namespace = namespace
        self.type = type
        self.ann = ann  # An object to pretty print with the load

    def sources(self) -> List[Value]:
        return []

    def to_str(self, env: Environment) -> str:
        ann = '  ({})'.format(repr(self.ann)) if self.ann else ''
        name = self.identifier
        if self.module_name is not None:
            name = '{}.{}'.format(self.module_name, name)
        return env.format('%r = %s :: %s%s', self, name, self.namespace, ann)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_load_static(self)


class TupleSet(RegisterOp):
    """dest = (reg, ...) (for fixed-length tuple)"""

    error_kind = ERR_NEVER

    def __init__(self, items: List[Value], line: int) -> None:
        super().__init__(line)
        self.items = items
        self.tuple_type = RTuple([arg.type for arg in items])
        self.type = self.tuple_type

    def sources(self) -> List[Value]:
        return self.items[:]

    def to_str(self, env: Environment) -> str:
        item_str = ', '.join(env.format('%r', item) for item in self.items)
        return env.format('%r = (%s)', self, item_str)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_tuple_set(self)


class TupleGet(RegisterOp):
    """dest = src[n] (for fixed-length tuple)"""

    error_kind = ERR_NEVER

    def __init__(self, src: Value, index: int, line: int) -> None:
        super().__init__(line)
        self.src = src
        self.index = index
        assert isinstance(src.type, RTuple), "TupleGet only operates on tuples"
        self.type = src.type.types[index]

    def sources(self) -> List[Value]:
        return [self.src]

    def to_str(self, env: Environment) -> str:
        return env.format('%r = %r[%d]', self, self.src, self.index)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_tuple_get(self)


class Cast(RegisterOp):
    """dest = cast(type, src)

    Perform a runtime type check (no representation or value conversion).

    DO NOT increment reference counts.
    """

    error_kind = ERR_MAGIC

    def __init__(self, src: Value, typ: RType, line: int) -> None:
        super().__init__(line)
        self.src = src
        self.type = typ

    def sources(self) -> List[Value]:
        return [self.src]

    def to_str(self, env: Environment) -> str:
        return env.format('%r = cast(%s, %r)', self, self.type, self.src)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_cast(self)

    def safe_to_optimize_out(self) -> bool:
        return True


class Box(RegisterOp):
    """dest = box(type, src)

    This converts from a potentially unboxed representation to a straight Python object.
    Only supported for types with an unboxed representation.
    """

    error_kind = ERR_NEVER

    def __init__(self, src: Value, line: int = -1) -> None:
        super().__init__(line)
        self.src = src
        self.type = object_rprimitive

    def sources(self) -> List[Value]:
        return [self.src]

    def to_str(self, env: Environment) -> str:
        return env.format('%r = box(%s, %r)', self, self.src.type, self.src)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_box(self)


class Unbox(RegisterOp):
    """dest = unbox(type, src)

    This is similar to a cast, but it also changes to a (potentially) unboxed runtime
    representation. Only supported for types with an unboxed representation.
    """

    error_kind = ERR_MAGIC

    def __init__(self, src: Value, typ: RType, line: int) -> None:
        super().__init__(line)
        self.src = src
        self.type = typ

    def sources(self) -> List[Value]:
        return [self.src]

    def to_str(self, env: Environment) -> str:
        return env.format('%r = unbox(%s, %r)', self, self.type, self.src)

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_unbox(self)


class RaiseStandardError(RegisterOp):
    """Raise built-in exception with an optional error string.

    We have a separate opcode for this for convenience and to
    generate smaller, more idiomatic C code.
    """

    # TODO: Make it more explicit at IR level that this always raises

    error_kind = ERR_FALSE

    VALUE_ERROR = 'ValueError'
    ASSERTION_ERROR = 'AssertionError'

    def __init__(self, class_name: str, message: Optional[str], line: int) -> None:
        super().__init__(line)
        self.class_name = class_name
        self.message = message
        self.type = bool_rprimitive

    def to_str(self, env: Environment) -> str:
        if self.message is not None:
            return 'raise %s(%r)' % (self.class_name, self.message)
        else:
            return 'raise %s' % self.class_name

    def sources(self) -> List[Value]:
        return []

    def accept(self, visitor: 'OpVisitor[T]') -> T:
        return visitor.visit_raise_standard_error(self)


class RuntimeArg:
    def __init__(self, name: str, typ: RType) -> None:
        self.name = name
        self.type = typ

    def __repr__(self) -> str:
        return 'RuntimeArg(name=%s, type=%s)' % (self.name, self.type)


class FuncSignature:
    # TODO: track if method?
    def __init__(self, args: Sequence[RuntimeArg], ret_type: RType) -> None:
        self.args = tuple(args)
        self.ret_type = ret_type

    def __repr__(self) -> str:
        return 'FuncSignature(args=%r, ret=%r)' % (self.args, self.ret_type)


class FuncIR:
    """Intermediate representation of a function with contextual information."""

    def __init__(self,
                 name: str,
                 class_name: Optional[str],
                 module_name: str,
                 sig: FuncSignature,
                 blocks: List[BasicBlock],
                 env: Environment) -> None:
        self.name = name
        self.class_name = class_name
        self.module_name = module_name
        self.blocks = blocks
        self.env = env
        self.sig = sig

    @property
    def args(self) -> Sequence[RuntimeArg]:
        return self.sig.args

    @property
    def ret_type(self) -> RType:
        return self.sig.ret_type

    def cname(self, names: NameGenerator) -> str:
        name = self.name
        if self.class_name:
            name += '_' + self.class_name
        return names.private_name(self.module_name, name)

    def __str__(self) -> str:
        return '\n'.join(format_func(self))


INVALID_FUNC_DEF = FuncDef('<INVALID_FUNC_DEF>', [], Block([]))


# Some notes on the vtable layout:
# Each concrete class has a vtable that contains function pointers for its
# methods and for getters/setters of its attributes. So that subclasses
# may be efficiently used when their parent class is expected, the layout
# of child vtables must be an extension of their base class's vtable.
#
# This makes multiple inheritance tricky, since obviously we cannot be
# an extension of multiple parent classes. We solve this by requriing
# all but one parent to be "traits", which we can operate on in a
# somewhat less efficient way. For each trait implemented by a class,
# we generate a separate vtable for the methods in that trait.
# We then store an array of (trait type, trait vtable) pointers alongside
# a class's main vtable. When we want to call a trait method, we
# (at runtime!) search the array of trait vtables to find the correct one,
# then call through it.
#
# To keep down the number of indirections necessary, we store the
# array of trait vtables in the memory *before* the class vtable, and
# search it backwards.  (This is a trick we can only do once---there
# are only two directions to store data in---but I don't think we'll
# need it again.)
# There are some tricks we could try in the future to store the trait
# vtables inline in the trait table (which would cut down one indirection),
# but this seems good enough for now.#
#
# As an example:
# Imagine that we have a class B that inherits from a concrete class A
# and traits T1 and T2, and that A has attribute x and methods foo() and
# bar() and B overrides bar() with a more specific type.
# Then B's vtable will look something like:
#
#      T1 type object
#      ptr to B's T1 trait vtable
#      T2 type object
#      ptr to B's T2 trait vtable
# -> | Getter for x
#    | Setter for x
#    | A.foo
#    | Glue function that converts between A.bar's type and B.bar
#      B.bar
#      B.baz
#
# The arrow points to the "start" of the vtable (what vtable pointers
# point to) and the bars indicate which parts correspond to the parent
# class A's vtable layout.

# Descriptions of method and attribute entries in class vtables.
# The 'cls' field is the class that the method/attr was defined in,
# which might be a parent class.

VTableMethod = NamedTuple(
    'VTableMethod', [('cls', 'ClassIR'),
                     ('name', str),
                     ('method', FuncIR)])


VTableAttr = NamedTuple(
    'VTableAttr', [('cls', 'ClassIR'),
                   ('name', str),
                   ('is_setter', bool)])


VTableEntries = List[Union[VTableMethod, VTableAttr]]


class ClassIR:
    """Intermediate representation of a class.

    This also describes the runtime structure of native instances.
    """
    def __init__(self, name: str, module_name: str, is_trait: bool = False) -> None:
        self.name = name
        self.module_name = module_name
        self.is_trait = is_trait
        self.attributes = OrderedDict()  # type: OrderedDict[str, RType]
        # We populate method_types with the signatures of every method before
        # we generate methods, and we rely on this information being present.
        self.method_types = OrderedDict()  # type: OrderedDict[str, FuncSignature]
        self.methods = OrderedDict()  # type: OrderedDict[str, FuncIR]
        # Glue methods for boxing/unboxing when a class changes the type
        # while overriding a method. Maps from (parent class overrided, method)
        # to IR of glue method.
        self.glue_methods = {}  # type: Dict[Tuple[ClassIR, str], FuncIR]
        self.vtable = None  # type: Optional[Dict[str, int]]
        self.vtable_entries = []  # type: VTableEntries
        self.trait_vtables = OrderedDict()  # type: OrderedDict[ClassIR, VTableEntries]
        # N.B: base might not actually quite be the direct base.
        # It is the nearest concrete base, but we allow a trait in between.
        self.base = None  # type: Optional[ClassIR]
        self.traits = []  # type: List[ClassIR]
        # Supply a working mro for most generated classes. Real classes will need to
        # fix it up.
        self.mro = [self]  # type: List[ClassIR]
        # base_mro is the chain of concrete (non-trait) ancestors
        self.base_mro = [self]  # type: List[ClassIR]

    def real_base(self) -> Optional['ClassIR']:
        """Return the actual concrete base class, if there is one."""
        if len(self.mro) > 1 and not self.mro[1].is_trait:
            return self.mro[1]
        return None

    def vtable_entry(self, name: str) -> int:
        assert self.vtable is not None, "vtable not computed yet"
        assert name in self.vtable, '%r has no attribute %r' % (self.name, name)
        return self.vtable[name]

    def attr_type(self, name: str) -> RType:
        for ir in self.mro:
            if name in ir.attributes:
                return ir.attributes[name]
        assert False, '%r has no attribute %r' % (self.name, name)

    def method_sig(self, name: str) -> FuncSignature:
        for ir in self.mro:
            if name in ir.method_types:
                return ir.method_types[name]
        assert False, '%r has no method %r' % (self.name, name)

    def name_prefix(self, names: NameGenerator) -> str:
        return names.private_name(self.module_name, self.name)

    def struct_name(self, names: NameGenerator) -> str:
        return '{}Object'.format(self.name_prefix(names))

    def get_method(self, name: str) -> Optional[FuncIR]:
        for ir in self.mro:
            if name in ir.methods:
                return ir.methods[name]
        return None


INVALID_CLASS = ClassIR('<INVALID_CLASS>', '')


LiteralsMap = Dict[Tuple[Type[object], Union[int, float, str, bytes]], str]


class ModuleIR:
    """Intermediate representation of a module."""

    def __init__(self,
            imports: List[str],
            from_imports: Dict[str, List[Tuple[str, str]]],
            literals: LiteralsMap,
            functions: List[FuncIR],
            classes: List[ClassIR]) -> None:
        self.imports = imports[:]
        self.from_imports = from_imports
        self.literals = literals
        self.functions = functions
        self.classes = classes

        if 'builtins' not in self.imports:
            self.imports.insert(0, 'builtins')


class OpVisitor(Generic[T]):
    @abstractmethod
    def visit_goto(self, op: Goto) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_branch(self, op: Branch) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_return(self, op: Return) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_unreachable(self, op: Unreachable) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_primitive_op(self, op: PrimitiveOp) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_assign(self, op: Assign) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_load_int(self, op: LoadInt) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_load_error_value(self, op: LoadErrorValue) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_get_attr(self, op: GetAttr) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_set_attr(self, op: SetAttr) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_load_static(self, op: LoadStatic) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_tuple_get(self, op: TupleGet) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_tuple_set(self, op: TupleSet) -> T:
        raise NotImplementedError

    def visit_inc_ref(self, op: IncRef) -> T:
        raise NotImplementedError

    def visit_dec_ref(self, op: DecRef) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_call(self, op: Call) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_method_call(self, op: MethodCall) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_cast(self, op: Cast) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_box(self, op: Box) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_unbox(self, op: Unbox) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_raise_standard_error(self, op: RaiseStandardError) -> T:
        raise NotImplementedError


def format_blocks(blocks: List[BasicBlock], env: Environment) -> List[str]:
    # First label all of the blocks
    for i, block in enumerate(blocks):
        block.label = i

    handler_map = {}  # type: Dict[BasicBlock, List[BasicBlock]]
    for b in blocks:
        if b.error_handler:
            handler_map.setdefault(b.error_handler, []).append(b)

    lines = []
    for i, block in enumerate(blocks):
        i == len(blocks) - 1

        handler_msg = ''
        if block in handler_map:
            labels = sorted(env.format('%l', b.label) for b in handler_map[block])
            handler_msg = ' (handler for {})'.format(', '.join(labels))

        lines.append(env.format('%l:%s', block.label, handler_msg))
        ops = block.ops
        if (isinstance(ops[-1], Goto) and i + 1 < len(blocks) and
                ops[-1].label == blocks[i + 1]):
            # Hide the last goto if it just goes to the next basic block.
            ops = ops[:-1]
        for op in ops:
            line = '    ' + op.to_str(env)
            lines.append(line)

        if not isinstance(block.ops[-1], (Goto, Branch, Return, Unreachable)):
            # Each basic block needs to exit somewhere.
            lines.append('    [MISSING BLOCK EXIT OPCODE]')
    return lines


def format_func(fn: FuncIR) -> List[str]:
    lines = []
    cls_prefix = fn.class_name + '.' if fn.class_name else ''
    lines.append('def {}{}({}):'.format(cls_prefix, fn.name,
                                        ', '.join(arg.name for arg in fn.args)))
    for line in fn.env.to_lines():
        lines.append('    ' + line)
    code = format_blocks(fn.blocks, fn.env)
    lines.extend(code)
    return lines


def is_empty_module_top_level(fn: FuncIR) -> bool:
    return fn.name == TOP_LEVEL_NAME and len(fn.blocks) == 1 and len(fn.blocks[0].ops) == 2


class RTypeVisitor(Generic[T]):
    @abstractmethod
    def visit_rprimitive(self, typ: RPrimitive) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_rinstance(self, typ: RInstance) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_roptional(self, typ: ROptional) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_rtuple(self, typ: RTuple) -> T:
        raise NotImplementedError

    @abstractmethod
    def visit_rvoid(self, typ: RVoid) -> T:
        raise NotImplementedError


# Import various modules that set up global state.
import mypyc.ops_int
import mypyc.ops_str
import mypyc.ops_list
import mypyc.ops_dict
import mypyc.ops_tuple
import mypyc.ops_misc
