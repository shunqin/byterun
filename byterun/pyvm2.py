# pyvm2 by Paul Swartz (z3p), from http://www.twistedmatrix.com/users/z3p/
"""
Security issues in running an open python interpreter:

1. importing modules (sys, os, perhaps anything implemented in C?)
2. large objects (range(999999))
3. heavy math (999999**99999)
4. infinite loops (while 1: pass)

I think the policy should be "disallow by default" and if users complain,
the feature can be added.

Solutions to above problems:
1.  only allow access to certian parts of modules / disallow the module entirely
    see SysModule() below, maybe turn it into a generic module wrapper?
2.  this was solved by checking for range in allowCFunction
    are there other C methods that may return large objects?
3.  the only way I can see to fix this is a check in BINARY_POW/INPLACE_POW
    perhaps also check BINARY_MUL/INPLACE_MUL?  I think the ADD/SUB/MOD/XOR
    functions are OK.
4.  this was solved with the absolute byte code counter.

Maybe I should follow the RExec interface?  I think the security issues were
in how it was implemented, not in the API.
On second thought, their API is very strange.

Ideas for a Python VM written in Python:
1.  running the byte codes as a specific user (useful for running multiple
    python interpreters for multiple users in one process)
2.  faked threading.  run a set number of byte codes, then switch to a second 'Thread' and run the byte codes, repeat.
"""

import operator, dis, new, inspect, copy, sys, types
CO_GENERATOR = 32 # flag for "this code uses yield"

class Cell:
    dontAccess = ['deref', 'set', 'get']
    def __init__(self):
        self.deref = None

    def set(self, deref):
        self.deref = deref

    def get(self):
        return self.deref

class Frame:
    readOnly = ['f_back', 'f_code', 'f_locals', 'f_globals', 'f_builtins',
                'f_restricted', 'f_lineno', 'f_lasti']
    dontAccess = ['_vm', '_cells', '_blockStack', '_generator']

    def __init__(self, f_code, f_globals, f_locals, vm):
        self._vm = vm
        self.f_code = f_code
        self.f_globals = f_globals
        self.f_locals = f_locals
        self.f_back = vm.frame()
        if self.f_back:
            self.f_builtins = self.f_back.f_builtins
        else:
            self.f_builtins = f_locals['__builtins__']
            if hasattr(self.f_builtins, '__dict__'):
                self.f_builtins = self.f_builtins.__dict__
        self.f_restricted = 1
        self.f_lineno = f_code.co_firstlineno
        self.f_lasti = 0
        if f_code.co_cellvars:
            self._cells = {}
            if not self.f_back._cells:
                self.f_back._cells = {}
            for var in f_code.co_cellvars:
                self.f_back._cells[var] = self._cells[var] = Cell()
                if self.f_locals.has_key(var):
                    self._cells[var].set(self.f_locals[var])
        else:
            self._cells = None
        if f_code.co_freevars:
            if not self._cells:
                self._cells = {}
            for var in f_code.co_freevars:
                self._cells[var] = self.f_back._cells[var]
        self._blockStack = []

    def __str__(self):
        return '<frame object at 0x%08X>' % id(self)

class Generator:
    readOnly = ['gi_frame', 'gi_running']
    dontAccess = ['_vm', '_savedstack']

    def __init__(self, g_frame, vm):
        self.gi_frame = g_frame
        self.gi_running = 0
        self._vm = copy.copy(vm)
        self._vm._stack = []
        self._vm._returnValue = None
        self._vm._lastException = (None, None, None)
        self._vm._frames = copy.copy(vm._frames)
        self._vm._frames.append(g_frame)

    def __iter__(self):
        return self

    def next(self):
        self.gi_running = 1
        return self._vm.run()

class Class:
    dontAccess = ['_name', '_bases', '_locals']
    def __init__(self, name, bases, methods):
        self._name = name
        self._bases = bases
        self._locals = methods
    def __call__(self, *args, **kw):
        return Object(self, self._name, self._bases, self._locals, args, kw)
    def __str__(self):
        return '<class %s at 0x%08X>' % (self._name, id(self))
    __repr__ = __str__
    def isparent(self, obj):
        if not isinstance(obj, Object):
            return False
        if obj._class is self:
            return True
        if self in obj._bases:
            return True
        return False

class Object:
    def __init__(self, _class, name, bases, methods, args, kw):
        self._class = _class
        self._name = name
        self._bases = bases
        self._locals = methods
        if methods.has_key('__init__'):
            methods['__init__'](self, *args, **kw)
    def __str__(self):
        return '<%s instance at 0x%08X>' % (self._name, id(self))
    __repr__ = __str__
        
class Method:
    readOnly = ['im_self', 'im_class', 'im_func']
    def __init__(self, object, _class, func):
        self.im_self = object
        self.im_class = _class
        self.im_func = func
    def __str__(self):
        if self.im_self:
            return '<bound method %s.%s of %s>' % (self.im_self._name,
                                                   self.im_func.func_name,
                                                   str(self.im_self))
        else:
            return '<unbound method %s.%s>' % (self.im_class._name,
                                               self.im_func.func_name)
    __repr__ = __str__

UNARY_OPERATORS = {
    'POSITIVE': operator.pos,
    'NEGATIVE': operator.neg,
    'NOT':      operator.not_,
    'CONVERT':  repr,
    'INVERT':   operator.invert,
}

BINARY_OPERATORS = {
    'POWER':    pow,
    'MULTIPLY': operator.mul,
    'DIVIDE':   operator.div,
    'MODULO':   operator.mod,
    'ADD':      operator.add,
    'SUBTRACT': operator.sub,
    'SUBSCR':   operator.getitem,
    'LSHIFT':   operator.lshift,
    'RSHIFT':   operator.rshift,
    'AND':      operator.and_,
    'XOR':      operator.xor,
    'OR':       operator.or_,
}

INPLACE_OPERATORS = { # these are execed
    'POWER':    'x**=y',
    'MULTIPLY': 'x*=y',
    'DIVIDE':   'x/=y',
    'MODULO':   'x%=y',
    'ADD':      'x+=y',
    'SUBTRACT': 'x-=y',
    'LSHIFT':   'x>>=y',
    'RSHIFT':   'x<<=y',
    'AND':      'x&=y',
    'XOR':      'x^=y',
    'OR':       'x|=y',
}
    
COMPARE_OPERATORS = [
    operator.lt,
    operator.le,
    operator.eq,
    operator.ne,
    operator.gt,
    operator.ge,
    operator.contains,
    lambda x,y: x not in y,
    lambda x,y: x is y,
    lambda x,y: x is not y,
    lambda x,y: issubclass(x, Exception) and issubclass(x, y)
]
    

class VirtualMachineError(Exception):
    """For raising errors in the operation of the VM."""
    pass

class VirtualMachine:
    def __init__(self):
        self._frames = [] # list of current stack frames
        self._stack = [] # current stack
        self._returnValue = None
        self._lastException = (None, None, None)
        self._log = []

    def frame(self):
        return self._frames and self._frames[-1] or None

    def pop(self):
        return self._stack.pop()
    def push(self, thing):
        self._stack.append(thing)

    def log(self, msg):
        self._log.append(msg)

    def loadCode(self, code, args=[], kw={}, f_globals=None, f_locals=None):
        self.log("loadCode: code=%r, args=%r, kw=%r, f_globals=%r, f_locals=%r" % (code, args, kw, f_globals, f_locals))
        if f_globals:
            f_globals = f_globals
            if not f_locals:
                f_locals = f_globals
        elif self.frame():
            f_globals = self.frame().f_globals
            f_locals = {}
        else:
            f_globals = f_locals = globals()
        for i in range(code.co_argcount):
            name = code.co_varnames[i]
            if i < len(args):
                if kw.has_key(name):
                    raise TypeError("got multiple values for keyword argument '%s'" % name)
                else:
                    f_locals[name] = args[i]
            else:
                if kw.has_key(name):
                    f_locals[name] = kw[name]
                else:
                    raise TypeError("did not get value for argument '%s'" % name)
        frame = Frame(code, f_globals, f_locals, self)
        self._frames.append(frame)

    def run(self):
        while self._frames:
            # pre will go here
            frame = self.frame()
            opoffset = frame.f_lasti
            byte = frame.f_code.co_code[opoffset]
            frame.f_lasti += 1
            byteCode = ord(byte)
            byteName = dis.opname[byteCode]
            arg = None
            arguments = []
            if byteCode >= dis.HAVE_ARGUMENT:
                arg = frame.f_code.co_code[frame.f_lasti:frame.f_lasti+2]
                frame.f_lasti += 2
                intArg = ord(arg[0]) + (ord(arg[1])<<8)
                if byteCode in dis.hasconst:
                    arg = frame.f_code.co_consts[intArg]
                elif byteCode in dis.hasfree:
                    if intArg < len(frame.f_code.co_cellvars):
                        arg = frame.f_code.co_cellvars[intArg]
                    else:
                        arg = frame.f_code.co_freevars[intArg-len(frame.f_code.co_cellvars)]
                elif byteCode in dis.hasname:
                    arg = frame.f_code.co_names[intArg]
                elif byteCode in dis.hasjrel:
                    arg = frame.f_lasti + intArg
                elif byteCode in dis.hasjabs:
                    arg = intArg
                elif byteCode in dis.haslocal:
                    arg = frame.f_code.co_varnames[intArg]
                else:
                    arg = intArg
                arguments = [arg]

            if 1:
                op = "%d: %s" % (opoffset, byteName)
                if arguments:
                    op += " %r" % (arguments[0],)
                self.log("%s%40s %r" % ("  "*(len(self._frames)-1), op, self._stack))

            finished = False
            try:
                if byteName.startswith('UNARY_'):
                    self.unaryOperator(byteName[6:])
                elif byteName.startswith('BINARY_'):
                    self.binaryOperator(byteName[7:])
                elif byteName.startswith('INPLACE_'):
                    self.inplaceOperator(byteName[8:])
                elif byteName.find('SLICE') != -1:
                    self.sliceOperator(byteName)
                else:
                    # dispatch
                    func = getattr(self, 'byte_%s' % byteName, None)
                    if not func:
                        raise VirtualMachineError("unknown bytecode type: %s" % byteName)
                    finished = func(*arguments)
                #print len(self._frames), self._stack
                if finished:
                    self._frames.pop()
                    self.push(self._returnValue)
                if self._lastException[0]:
                    self._lastException = (None, None, None)
            except:
                while self._frames and not self.frame()._blockStack:
                    self._frames.pop()
                if not self._frames:
                    raise
                self._lastException = sys.exc_info()[:2] + (None,)
                while True:
                    if not self._frames:
                        raise
                    block = self.frame()._blockStack.pop()
                    if block[0] in ('except', 'finally'):
                        if block[0] == 'except':
                            self.push(self._lastException[2])
                            self.push(self._lastException[1])
                            self.push(self._lastException[0])
                        self.frame().f_lasti = block[1]
                        break
                    while not self.frame()._blockStack:
                        self._frames.pop()
                        if not self._frames:
                            break

        # Get rid of the last returned value??
        self.pop()

        # Check some invariants
        if self._frames:
            raise VirtualMachineError("Frames left over!")
        if self._stack:
            raise VirtualMachineError("Data left on stack! %r" % self._stack)

        if self._lastException[0]:
            e1, e2, e3 = self._lastException
            raise e1, e2, e3
        return self._returnValue

    def unaryOperator(self, op):
        one = self.pop()
        self.push(UNARY_OPERATORS[op](one))

    def binaryOperator(self, op):
        one = self.pop()
        two = self.pop()
        self.push(BINARY_OPERATORS[op](two, one))

    def inplaceOperator(self, op):
        y = self.pop()
        x = self.pop()
        # Isn't there a better way than exec?? :(
        vars = {'x':x, 'y':y}
        exec INPLACE_OPERATORS[op] in vars
        self.push(vars['x'])

    def sliceOperator(self, op):
        start = 0
        end = None # we will take this to mean end
        op, count = op[:-2], int(op[-1])
        if count == 1:
            start = self.pop()
        elif count == 2:
            end = self.pop()
        elif count == 3:
            end = self.pop()
            start = self.pop()
        l = self.pop()
        if end == None:
            end = len(l)
        if op.startswith('STORE_'):
            l[start:end] = self.pop()
        elif op.startswith('DELETE_'):
            del l[start:end]
        else:
            self.push(l[start:end])

    def byte_DUP_TOP(self):
        item = self.pop()
        self.push(item)
        self.push(item)

    def byte_POP_TOP(self):
        self.pop()

    def byte_STORE_SUBSCR(self):
        ind = self.pop()
        l = self.pop()
        item = self.pop()
        l[ind] = item

    def byte_DELETE_SUBSCR(self):
        ind = self.pop()
        l = self.pop()
        del l[ind]

    def byte_PRINT_EXPR(self):
        print self.pop()

    def byte_PRINT_ITEM(self):
        print self.pop(),

    def byte_PRINT_ITEM_TO(self):
        item = self.pop()
        to = self.pop()
        print >>to, item,
        self.push(to)

    def byte_PRINT_NEWLINE(self):
        print

    def byte_PRINT_ITEM_TO(self):
        # TODO: looks like PRINT_NEWLINE_TO to me...
        to = self.pop()
        print >>to , ''
        self.push(to)

    def byte_BREAK_LOOP(self):
        block = self.frame()._blockStack.pop()
        while block[0] != 'loop':
            block = self.frame()._blockStack.pop()
        self.frame().f_lasti = block[1]

    def byte_LOAD_LOCALS(self):
        self.push(self.frame().f_locals)

    def byte_RETURN_VALUE(self):
        self._returnValue = self.pop()
        return True
        if 0: # TODO: This makes one-line code fail. Part of aborted generator implementation?
            func = self.pop()
            self.push(func)
            if isinstance(func, Object):
                self._frames.pop()
            else:
                return True

    def byte_YIELD_VALUE(self):
        value = self.pop() # since we're running in a different VM
        self._returnValue = value
        return True

    def byte_IMPORT_STAR(self):
        # TODO: this doesn't use __all__ properly.
        mod = self.pop()
        for attr in dir(mod):
            if attr[0] != '_':
                self.frame().f_locals[attr] = getattr(mod, attr)

    def byte_EXEC_STMT(self):
        one = self.pop()
        two = self.pop()
        three = self.pop()
        exec three in two, one

    def byte_POP_BLOCK(self):
        self.frame()._blockStack.pop()

    def byte_END_FINALLY(self):
        if self._lastException[0]:
            raise self._lastException[0], self._lastException[1], self._lastException[2]
        else:
            return True

    def byte_BUILD_CLASS(self):
        methods = self.pop()
        bases = self.pop()
        name = self.pop()
        self.push(Class(name, bases, methods))

    def byte_STORE_NAME(self, name):
        self.frame().f_locals[name] = self.pop()

    def byte_DELETE_NAME(self, name):
        del self.frame().f_locals[name]

    def byte_UNPACK_SEQUENCE(self, count):
        l = self.pop()
        for i in range(len(l)-1, -1, -1):
            self.push(l[i])

    def byte_DUP_TOPX(self, count):
        items = [self.pop() for i in range(count)]
        items.reverse()
        [self.push(i) for i in items]

    def byte_STORE_ATTR(self, name):
        obj = self.pop()
        setattr(obj, name, self.pop())

    def byte_DELETE_ATTR(self, name):
        obj = self.pop()
        delattr(obj, name)

    def byte_LOAD_CONST(self, const):
        self.push(const)

    def byte_LOAD_NAME(self, name):
        frame = self.frame()
        if frame.f_locals.has_key(name):
            item = frame.f_locals[name]
        elif frame.f_globals.has_key(name):
            item = frame.f_globals[name]
        elif frame.f_builtins.has_key(name):
            item = frame.f_builtins[name]
        else:
            raise NameError("name '%s' is not defined" % name)
        self.push(item)

    def byte_BUILD_TUPLE(self, count):
        elts = [self.pop() for i in xrange(count)]
        elts.reverse()
        self.push(tuple(elts))

    def byte_BUILD_LIST(self, count):
        elts = [self.pop() for i in xrange(count)]
        elts.reverse()
        self.push(elts)

    def byte_BUILD_MAP(self, size):
        # size is ignored.
        self.push({})

    def byte_STORE_MAP(self):
        key = self.pop()
        value = self.pop()
        the_map = self.pop()
        the_map[key] = value
        self.push(the_map)

    def byte_LOAD_ATTR(self, attr):
        obj = self.pop()
        if isinstance(obj, (Object, Class)):
            val = obj._locals[attr]
        else:
            val = getattr(obj, attr)
        if isinstance(obj, Object) and isinstance(val, Function):
            val = Method(obj, obj._class, val)
        elif isinstance(obj, Class) and isinstance(val, Function):
            val = Method(None, obj, val)
        self.push(val)

    def byte_COMPARE_OP(self, opnum):
        one = self.pop()
        two = self.pop()
        self.push(COMPARE_OPERATORS[opnum](two, one))

    def byte_IMPORT_NAME(self, name):
        fromlist = self.pop()
        level = self.pop()
        frame = self.frame()
        self.push(__import__(name, frame.f_globals, frame.f_locals, fromlist, level))

    def byte_IMPORT_FROM(self, name):
        mod = self.pop()
        self.push(mod)
        self.push(getattr(mod, name))

    def byte_JUMP_IF_TRUE(self, jump):
        val = self.pop()
        self.push(val)
        if val:
            self.frame().f_lasti = jump

    def byte_POP_JUMP_IF_TRUE(self, jump):
        val = self.pop()
        if val:
            self.frame().f_lasti = jump

    def byte_JUMP_IF_FALSE(self, jump):
        val = self.pop()
        self.push(val)
        if not val:
            self.frame().f_lasti = jump

    def byte_POP_JUMP_IF_FALSE(self, jump):
        val = self.pop()
        if not val:
            self.frame().f_lasti = jump

    def byte_JUMP_FORWARD(self, jump):
        self.frame().f_lasti = jump

    def byte_JUMP_ABSOLUTE(self, jump):
        self.frame().f_lasti = jump

    def byte_LOAD_GLOBAL(self, name):
        f = self.frame()
        if f.f_globals.has_key(name):
            self.push(f.f_globals[name])
        elif f.f_builtins.has_key(name):
            self.push(f.f_builtins[name])
        else:
            raise NameError("global name '%s' is not defined" % name)

    def byte_SETUP_LOOP(self, dest):
        self.frame()._blockStack.append(('loop', dest))

    def byte_SETUP_EXCEPT(self, dest):
        self.frame()._blockStack.append(('except', dest))

    def byte_SETUP_FINALLY(self, dest):
        self.frame()._blockStack.append(('finally', dest))

    def byte_LOAD_FAST(self, name):
        self.push(self.frame().f_locals[name])

    def byte_STORE_FAST(self, name):
        self.frame().f_locals[name] = self.pop()

    def byte_LOAD_CLOSURE(self, name):
        self.push(self.frame()._cells[name])

    def byte_LOAD_DEREF(self, name):
        self.push(self.frame()._cells[name].get())

    def byte_SET_LINENO(self, lineno):
        self.frame().f_lineno = lineno

    def byte_CALL_FUNCTION(self, arg):
        return self.call_function(arg, [], {})

    def byte_CALL_FUNCTION_VAR(self, arg):
        args = self.pop()
        return self.call_function(arg, args, {})

    def byte_CALL_FUNCTION_KW(self, arg):
        kwargs = self.pop()
        return self.call_function(arg, [], kwargs)

    def byte_CALL_FUNCTION_VAR_KW(self, arg):
        kwargs = self.pop()
        args = self.pop()
        return self.call_function(arg, args, kwargs)

    def call_function(self, arg, args, kwargs):
        lenKw, lenPos = divmod(arg, 256)
        namedargs = {}
        for i in xrange(lenKw):
            value = self.pop()
            key = self.pop()
            namedargs[key] = value
        namedargs.update(kwargs)
        # TODO: Make a helper method for this
        posargs = []
        for i in xrange(lenPos):
            posargs.insert(0, self.pop())
        posargs.extend(args)

        func = self.pop()
        frame = self.frame()
        if hasattr(func, 'im_func'): # method
            if func.im_self:
                posargs.insert(0, func.im_self)
            if not func.im_class.isparent(posargs[0]):
                raise TypeError(
                    'unbound method %s() must be called with %s instance as first argument (got %s instead)' % 
                    (func.im_func.func_name, func.im_class._name, type(posargs[0]))
                )
            func = func.im_func
        if hasattr(func, 'func_code'):
            callargs = inspect.getcallargs(func, *posargs, **namedargs)
            self.loadCode(func.func_code, [], callargs)
            if func.func_code.co_flags & CO_GENERATOR:
                raise VirtualMachineError("cannot do generators ATM")
                gen = Generator(self.frame(), self)
                self._frames.pop()._generator = gen
                self.push(gen)
        else:
            self.push(func(*posargs, **namedargs))

    def byte_MAKE_FUNCTION(self, argc):
        code = self.pop()
        defaults = []
        for i in xrange(argc):
            defaults.insert(0, self.pop())
        globs = self.frame().f_globals
        fn = types.FunctionType(code, globs, argdefs=tuple(defaults))
        self.push(fn)

    def byte_MAKE_CLOSURE(self, argc):
        code = self.pop()
        defaults = []
        for i in xrange(argc):
            defaults.insert(0, self.pop())
        closure = []
        if code.co_freevars:
            for i in code.co_freevars:
                closure.insert(0, self.pop())
        self.push(Function(code, None, defaults, closure, self))

    def byte_RAISE_VARARGS(self, argc):
        if argc == 0:
            raise VirtualMachineError("Not implemented: re-raise")
        elif argc == 1:
            raise self.pop()
        elif argc == 2:
            raise self.pop(), self.pop()
        elif argc == 3:
            raise self.pop(), self.pop(), self.pop()

    def byte_GET_ITER(self):
        self.push(iter(self.pop()))

    def byte_FOR_ITER(self, jump):
        iterobj = self.pop()
        self.push(iterobj)
        #if not isinstance(iterobj, (Class, Object): # these are unsafe
                                                     # i.e. defined by user
        try:
            v = iterobj.next()
            self.push(v)
        except StopIteration:
            self.pop()
            self.frame().f_lasti = jump
