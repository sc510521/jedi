"""
Searching for names with given scope and name. This is very central in Jedi and
Python. The name resolution is quite complicated with descripter,
``__getattribute__``, ``__getattr__``, ``global``, etc.

Flow checks
+++++++++++

Flow checks are not really mature. There's only a check for ``isinstance``.  It
would check whether a flow has the form of ``if isinstance(a, type_or_tuple)``.
Unfortunately every other thing is being ignored (e.g. a == '' would be easy to
check for -> a is a string). There's big potential in these checks.
"""
from itertools import chain

from jedi._compatibility import hasattr, unicode, u
from jedi.parser import tree as pr
from jedi.parser import fast
from jedi import debug
from jedi import common
from jedi import settings
from jedi.evaluate import representation as er
from jedi.evaluate import dynamic
from jedi.evaluate import compiled
from jedi.evaluate import docstrings
from jedi.evaluate import iterable
from jedi.evaluate import imports
from jedi.evaluate import analysis
from jedi.evaluate import flow_analysis
from jedi.evaluate import param
from jedi.evaluate import helpers
from jedi.evaluate.cache import memoize_default


def filter_definition_names(names, position=None):
    # Just calculate the scope from the first
    stmt = names[0].get_definition()
    scope = stmt.get_parent_scope()
    if isinstance(stmt, (pr.CompFor, pr.Lambda, pr.GlobalStmt)):
        return names

    # Private name mangling (compile.c) disallows access on names
    # preceeded by two underscores `__` if used outside of the class. Names
    # that also end with two underscores (e.g. __id__) are not affected.
    names = list(names)
    for name in names:
        if name.value.startswith('__') and not name.value.endswith('__'):
            if filter_private_variable(scope, name):
                names.remove(name)

    if not (isinstance(scope, er.FunctionExecution)
            and isinstance(scope.base, er.LambdaWrapper)):
        names = pr.filter_after_position(names, position)
    return [name for name in names if name.is_definition()]


class NameFinder(object):
    def __init__(self, evaluator, scope, name_str, position=None):
        self._evaluator = evaluator
        # Make sure that it's not just a syntax tree node.
        self.scope = er.wrap(evaluator, scope)
        self.name_str = name_str
        self.position = position

    @debug.increase_indent
    def find(self, scopes, search_global=False):
        names = self.filter_name(scopes, search_global)
        types = self._names_to_types(names)

        if not names and not types \
                and not (isinstance(self.name_str, pr.Name)
                         and isinstance(self.name_str.parent.parent, pr.Param)):
            if not isinstance(self.name_str, (str, unicode)):  # TODO Remove?
                if search_global:
                    message = ("NameError: name '%s' is not defined."
                               % self.name_str)
                    analysis.add(self._evaluator, 'name-error', self.name_str,
                                 message)
                else:
                    analysis.add_attribute_error(self._evaluator,
                                                 self.scope, self.name_str)

        debug.dbg('finder._names_to_types: %s -> %s', names, types)
        if isinstance(self.scope, (er.Class, er.Instance)) and not search_global:
            return self._resolve_descriptors(types)
        else:
            return types

    def scopes(self, search_global=False):
        if search_global:
            return global_names_dict_generator(self._evaluator, self.scope, self.position)
        else:
            return ((n, None) for n in self.scope.names_dicts(search_global))

    def names_dict_lookup(self, names_dict, position):
        def get_param(scope, el):
            if isinstance(el.parent, pr.Param) or isinstance(el.parent.parent, pr.Param):
                return scope.param_by_name(str(el))
            return el

        search_str = str(self.name_str)
        try:
            names = names_dict[search_str]
            if not names:  # We want names, otherwise stop.
                return []
        except KeyError:
            return []

        names = filter_definition_names(names, position)

        name_scope = None
        # Only the names defined in the last position are valid definitions.
        last_names = []
        for name in reversed(sorted(names, key=lambda name: name.start_pos)):
            stmt = name.get_definition()
            name_scope = er.wrap(self._evaluator, stmt.get_parent_scope())

            if isinstance(self.scope, er.Instance) and not isinstance(name_scope, er.Instance):
                # Instances should not be checked for positioning, because we
                # don't know in which order the functions are called.
                last_names.append(name)
                continue

            if isinstance(name_scope, compiled.CompiledObject):
                # Let's test this. TODO need comment. shouldn't this be
                # filtered before?
                last_names.append(name)
                continue

            if isinstance(name, compiled.CompiledName) \
                    or isinstance(name, er.InstanceName) and isinstance(name._origin_name, compiled.CompiledName):
                last_names.append(name)
                continue

            if isinstance(self.name_str, pr.Name):
                origin_scope = self.name_str.get_definition().parent
            else:
                origin_scope = None
            if isinstance(stmt.parent, compiled.CompiledObject):
                # TODO seriously? this is stupid.
                continue
            check = flow_analysis.break_check(self._evaluator, name_scope,
                                              stmt, origin_scope)
            if check is not flow_analysis.UNREACHABLE:
                last_names.append(name)
            if check is flow_analysis.REACHABLE:
                break

        if isinstance(name_scope, er.FunctionExecution):
            # Replace params
            return [get_param(name_scope, n) for n in last_names]
        return last_names

    def filter_name(self, names_dicts, search_global=False):
        """
        Searches names that are defined in a scope (the different
        `names_dicts`), until a name fits.
        """
        # TODO Now this import is really ugly. Try to remove it.
        # It's possibly the only api dependency.
        from jedi.api.interpreter import InterpreterNamespace
        names = []
        self.maybe_descriptor = isinstance(self.scope, er.Class)
        if not search_global and self.scope.isinstance(er.Function):
            return [n for n in self.scope.get_magic_function_names()
                    if str(n) == str(self.name_str)]

        scope_names_generator = []
        name_list_scope = None  # TODO delete
        for names_dict, position in names_dicts:
            names = self.names_dict_lookup(names_dict, position)
            if names:
                break
            #if isinstance(scope, (pr.Function, er.FunctionExecution)):
                #position = None

        # Need checked for now for the whole names_dict approach. That only
        # works on the first name_list_scope, the second one may be the same
        # with a different name set (e.g. ModuleWrapper yields the module
        # names first and after that it yields the properties that all modules
        # have like `__file__`, etc).
        checked = set()
        for name_list_scope, name_list in scope_names_generator:
            if name_list_scope not in checked and hasattr(name_list_scope, 'names_dict'):
                checked.add(name_list_scope)
                names = self.names_dict_lookup(name_list_scope, self.position)
                if names:
                    break
                if isinstance(name_list_scope, (pr.Function, er.FunctionExecution)):
                    self.position = None
                continue

            break_scopes = []
            if not isinstance(name_list_scope, compiled.CompiledObject):
                # Here is the position stuff happening (sorting of variables).
                # Compiled objects don't need that, because there's only one
                # reference.
                name_list = sorted(name_list, key=lambda n: n.start_pos, reverse=True)

            for name in name_list:
                if unicode(self.name_str) != unicode(name):
                    continue

                stmt = name.get_definition()
                scope = stmt.parent
                if scope in break_scopes:
                    continue
                # TODO create a working version for filtering private
                # variables.
                #if not search_global and filter_private_variable(self.scope, scope, name.value):
                #    filter_private_variable(name_list_scope, scope, name.value):
                #    continue

                # TODO we ignore a lot of elements here that should not be
                #   ignored. But then again flow_analysis also stops when the
                #   input scope is reached. This is not correct: variables
                #   might still have conditions if defined outside of the
                #   current scope.
                if isinstance(stmt, (pr.Param, pr.Import)) \
                        or isinstance(name_list_scope, (pr.Lambda, er.Instance, InterpreterNamespace)) \
                        or isinstance(scope, compiled.CompiledObject):
                    # Always reachable.
                    print('nons', name.get_parent_scope(), self.scope)
                    names.append(name)
                else:
                    print('yess', scope)
                    check = flow_analysis.break_check(self._evaluator,
                                                      name_list_scope,
                                                      stmt,
                                                      self.scope)
                    if check is not flow_analysis.UNREACHABLE:
                        names.append(name)
                    if check is flow_analysis.REACHABLE:
                        break

                if names and self._is_name_break_scope(stmt):
                    if self._does_scope_break_immediately(scope, name_list_scope):
                        break
                    else:
                        break_scopes.append(scope)
            if names:
                break

            if isinstance(self.scope, er.Instance):
                # After checking the dictionary of an instance (self
                # attributes), an attribute maybe a descriptor.
                self.maybe_descriptor = True

        scope_txt = (self.scope if self.scope == name_list_scope
                     else '%s-%s' % (self.scope, name_list_scope))
        debug.dbg('finder.filter_name "%s" in (%s): %s@%s', self.name_str,
                  scope_txt, u(names), self.position)
        return list(self._clean_names(names))

    def _clean_names(self, names):
        """
        ``NameFinder.filter_name`` should only output names with correct
        wrapper parents. We don't want to see AST classes out in the
        evaluation, so remove them already here!
        """
        for n in names:
            definition = n.parent
            if isinstance(definition, (pr.Function, pr.Class, pr.Module)):
                yield er.wrap(self._evaluator, definition).name
            else:
                yield n

    def _check_getattr(self, inst):
        """Checks for both __getattr__ and __getattribute__ methods"""
        result = []
        # str is important, because it shouldn't be `Name`!
        name = compiled.create(self._evaluator, str(self.name_str))
        with common.ignored(KeyError):
            result = inst.execute_subscope_by_name('__getattr__', name)
        if not result:
            # this is a little bit special. `__getattribute__` is executed
            # before anything else. But: I know no use case, where this
            # could be practical and the jedi would return wrong types. If
            # you ever have something, let me know!
            with common.ignored(KeyError):
                result = inst.execute_subscope_by_name('__getattribute__', name)
        return result

    def _is_name_break_scope(self, stmt):
        """
        Returns True except for nested imports and instance variables.
        """
        if stmt.isinstance(pr.ExprStmt):
            if isinstance(stmt, er.InstanceElement) and not stmt.is_class_var:
                return False
        elif isinstance(stmt, pr.Import) and stmt.is_nested():
            return False
        return True

    def _does_scope_break_immediately(self, scope, name_list_scope):
        """
        In comparison to everthing else, if/while/etc doesn't break directly,
        because there are multiple different places in which a variable can be
        defined.
        """
        if isinstance(scope, pr.Flow) \
                or isinstance(scope, pr.GlobalStmt):

            if isinstance(name_list_scope, er.Class):
                name_list_scope = name_list_scope.base
            return scope == name_list_scope
        else:
            return True

    def _names_to_types(self, names):
        types = []

        # Add isinstance and other if/assert knowledge.
        if isinstance(self.name_str, pr.Name):
            flow_scope = self.name_str.parent.parent
            # Ignore FunctionExecution parents for now.
            until = flow_scope.get_parent_until(er.FunctionExecution)
            while flow_scope and not isinstance(until, er.FunctionExecution):
                # TODO check if result is in scope -> no evaluation necessary
                n = check_flow_information(self._evaluator, flow_scope,
                                           self.name_str, self.position)
                if n:
                    return n
                flow_scope = flow_scope.parent

        for name in names:
            types += _name_to_types(self._evaluator, name, self.scope)
        if not names and isinstance(self.scope, er.Instance):
            # handling __getattr__ / __getattribute__
            types = self._check_getattr(self.scope)

        return types

    def _resolve_descriptors(self, types):
        """Processes descriptors"""
        #if not self.maybe_descriptor:
        #    return types
        result = []
        for r in types:
            try:
                desc_return = r.get_descriptor_returns
            except AttributeError:
                result.append(r)
            else:
                result += desc_return(self.scope)


            continue  # TODO DELETE WHAT FOLLOWS
            if isinstance(self.scope, (er.Instance, er.Class)) \
                    and hasattr(r, 'get_descriptor_returns'):
                # handle descriptors
                with common.ignored(KeyError):
                    result += r.get_descriptor_returns(self.scope)
                    continue
            result.append(r)
        return result


@memoize_default([], evaluator_is_first_arg=True)
def _name_to_types(evaluator, name, scope):
    types = []
    typ = name.get_definition()
    if typ.isinstance(pr.ForStmt):
        for_types = evaluator.eval_element(typ.children[-3])
        for_types = iterable.get_iterator_types(for_types)
        types += check_tuple_assignments(for_types, name)
    elif typ.isinstance(pr.CompFor):
        for_types = evaluator.eval_element(typ.children[3])
        for_types = iterable.get_iterator_types(for_types)
        types += check_tuple_assignments(for_types, name)
    elif isinstance(typ, pr.Param):
        types += _eval_param(evaluator, typ, scope)
    elif typ.isinstance(pr.ExprStmt):
        types += _remove_statements(evaluator, typ, name)
    elif typ.isinstance(pr.WithStmt):
        types += evaluator.eval_element(typ.node_from_name(name))
    elif isinstance(typ, pr.Import):
        types += imports.ImportWrapper(evaluator, name).follow()
    elif isinstance(typ, pr.GlobalStmt):
        types += evaluator.find_types(typ.get_parent_scope(), str(name))
    elif isinstance(typ, pr.TryStmt):
        # TODO an exception can also be a tuple. Check for those.
        # TODO check for types that are not classes and add it to
        # the static analysis report.
        exceptions = evaluator.eval_element(name.prev_sibling().prev_sibling())
        types = list(chain.from_iterable(
                     evaluator.execute(t) for t in exceptions))
    else:
        if typ.isinstance(er.Function):
            typ = typ.get_decorated_func()
        types.append(typ)
    return types


def _remove_statements(evaluator, stmt, name):
    """
    This is the part where statements are being stripped.

    Due to lazy evaluation, statements like a = func; b = a; b() have to be
    evaluated.
    """
    types = []
    # Remove the statement docstr stuff for now, that has to be
    # implemented with the evaluator class.
    #if stmt.docstr:
        #res_new.append(stmt)

    check_instance = None
    if isinstance(stmt, er.InstanceElement) and stmt.is_class_var:
        check_instance = stmt.instance
        stmt = stmt.var

    types += evaluator.eval_statement(stmt, seek_name=name)

    if check_instance is not None:
        # class renames
        types = [er.get_instance_el(evaluator, check_instance, a, True)
                 if isinstance(a, (er.Function, pr.Function))
                 else a for a in types]
    return types


def _eval_param(evaluator, param, scope):
    res_new = []
    func = param.parent

    cls = func.parent.get_parent_until((pr.Class, pr.Function))

    from jedi.evaluate.param import ExecutedParam, Arguments
    if isinstance(cls, pr.Class) and param.position_nr == 0 \
            and not isinstance(param, ExecutedParam):
        # This is where we add self - if it has never been
        # instantiated.
        if isinstance(scope, er.InstanceElement):
            res_new.append(scope.instance)
        else:
            inst = er.Instance(evaluator, er.wrap(evaluator, cls),
                               Arguments(evaluator, ()), is_generated=True)
            res_new.append(inst)
        return res_new

    # Instances are typically faked, if the instance is not called from
    # outside. Here we check it for __init__ functions and return.
    if isinstance(func, er.InstanceElement) \
            and func.instance.is_generated and str(func.name) == '__init__':
        param = func.var.params[param.position_nr]

    # Add docstring knowledge.
    doc_params = docstrings.follow_param(evaluator, param)
    if doc_params:
        return doc_params

    if isinstance(param, ExecutedParam):
        return res_new + param.eval(evaluator)
    else:
        # Param owns no information itself.
        res_new += dynamic.search_params(evaluator, param)
        if not res_new:
            if param.stars:
                t = 'tuple' if param.stars == 1 else 'dict'
                typ = evaluator.find_types(compiled.builtin, t)[0]
                res_new = evaluator.execute(typ)
        if param.default:
            res_new += evaluator.eval_element(param.default)
        return res_new


def check_flow_information(evaluator, flow, search_name_part, pos):
    """ Try to find out the type of a variable just with the information that
    is given by the flows: e.g. It is also responsible for assert checks.::

        if isinstance(k, str):
            k.  # <- completion here

    ensures that `k` is a string.
    """
    if not settings.dynamic_flow_information:
        return None

    result = []
    if flow.is_scope():
        for ass in reversed(flow.asserts):
            if pos is None or ass.start_pos > pos:
                continue
            result = _check_isinstance_type(evaluator, ass.assertion(), search_name_part)
            if result:
                break

    if isinstance(flow, (pr.IfStmt, pr.WhileStmt)):
        element = flow.children[1]
        result = _check_isinstance_type(evaluator, element, search_name_part)
    return result


def _check_isinstance_type(evaluator, element, search_name):
    try:
        assert element.type == 'power'
        # this might be removed if we analyze and, etc
        assert len(element.children) == 2
        first, trailer = element.children
        assert isinstance(first, pr.Name) and first.value == 'isinstance'
        assert trailer.type == 'trailer' and trailer.children[0] == '('
        assert len(trailer.children) == 3

        # arglist stuff
        arglist = trailer.children[1]
        args = param.Arguments(evaluator, arglist, trailer)
        lst = list(args.unpack())
        # Disallow keyword arguments
        assert len(lst) == 2 and lst[0][0] is None and lst[1][0] is None
        name = lst[0][1][0]  # first argument, values, first value
        # Do a simple get_code comparison. They should just have the same code,
        # and everything will be all right.
        classes = lst[1][1][0]
        call = helpers.call_of_name(search_name)
        assert name.get_code() == call.get_code()
    except AssertionError:
        return []

    result = []
    for typ in evaluator.eval_element(classes):
        for typ in (typ.values() if isinstance(typ, iterable.Array) else [typ]):
            result += evaluator.execute(typ)
    return result


def global_names_dict_generator(evaluator, scope, position):
    """
    For global lookups.
    """
    in_func = False
    while scope is not None:
        if not((scope.type == 'classdef' or isinstance(scope,
                compiled.CompiledObject) and scope.type() == 'class') and in_func):
            # Names in methods cannot be resolved within the class.

            for names_dict in scope.names_dicts(True):
                yield names_dict, position
            if scope.type == 'funcdef':
                # The position should be reset if the current scope is a function.
                in_func = True
                position = None
        scope = er.wrap(evaluator, scope.get_parent_scope())

    # Add builtins to the global scope.
    for names_dict in compiled.builtin.names_dicts(True):
        yield names_dict, None


def get_names_of_scope(evaluator, scope, position=None, star_search=True, include_builtin=True):
    """
    Get all completions (names) possible for the current scope. The star search
    option is only here to provide an optimization. Otherwise the whole thing
    would probably start a little recursive madness.

    This function is used to include names from outer scopes. For example, when
    the current scope is function:

    >>> from jedi._compatibility import u
    >>> from jedi.parser import Parser, load_grammar
    >>> parser = Parser(load_grammar(), u('''
    ... x = ['a', 'b', 'c']
    ... def func():
    ...     y = None
    ... '''))
    >>> scope = parser.module.subscopes[0]
    >>> scope
    <Function: func@3-5>

    `get_names_of_scope` is a generator.  First it yields names from most inner
    scope.

    >>> from jedi.evaluate import Evaluator
    >>> pairs = list(get_names_of_scope(Evaluator(load_grammar()), scope))
    >>> pairs[0]
    (<Function: func@3-5>, [<Name: y@4,4>])

    Then it yield the names from one level outer scope. For this example, this
    is the most outer scope.

    >>> pairs[1]
    (<ModuleWrapper: <SubModule: None@1-5>>, [<Name: x@2,0>, <Name: func@3,4>])

    After that we have a few underscore names that have been defined

    >>> pairs[2]
    (<ModuleWrapper: <SubModule: None@1-5>>, [<LazyName: __file__@0,0>, ...])


    Finally, it yields names from builtin, if `include_builtin` is
    true (default).

    >>> pairs[3]                                        #doctest: +ELLIPSIS
    (<Builtin: ...builtin...>, [<CompiledName: ...>, ...])

    :rtype: [(pr.Scope, [pr.Name])]
    :return: Return an generator that yields a pair of scope and names.
    """
    in_func_scope = scope
    origin_scope = scope
    while scope:
        # We don't want submodules to report if we have modules.
        # As well as some non-scopes, which are parents of list comprehensions.
        if isinstance(scope, pr.SubModule) and scope.parent or not scope.is_scope():
            scope = scope.parent
            continue

        # `pr.Class` is used, because the parent is never `Class`.
        # Ignore the Flows, because the classes and functions care for that.
        # InstanceElement of Class is ignored, if it is not the start scope.
        if not (scope != origin_scope and scope.isinstance(pr.Class)
                or scope.isinstance(er.Instance)
                and origin_scope.isinstance(er.Function, er.FunctionExecution)
                or isinstance(scope, compiled.CompiledObject)
                and scope.type() == 'class' and in_func_scope != scope):

            if isinstance(scope, (pr.SubModule, fast.Module)):
                scope = er.ModuleWrapper(evaluator, scope)

            for g in scope.scope_names_generator(position):
                yield g

        scope = scope.parent
        # This is used, because subscopes (Flow scopes) would distort the
        # results.
        if scope and scope.isinstance(er.Function, pr.Function, er.FunctionExecution):
            in_func_scope = scope
        if in_func_scope != scope \
                and isinstance(in_func_scope, (pr.Function, er.FunctionExecution)):
            position = None

    # Add builtins to the global scope.
    if include_builtin:
        yield compiled.builtin, compiled.builtin.get_defined_names()


def check_tuple_assignments(types, name):
    """
    Checks if tuples are assigned.
    """
    for index in name.assignment_indexes():
        new_types = []
        for r in types:
            try:
                func = r.get_exact_index_types
            except AttributeError:
                debug.warning("Invalid tuple lookup #%s of result %s in %s",
                              index, types, name)
            else:
                try:
                    new_types += func(index)
                except IndexError:
                    pass
        types = new_types
    return types


def filter_private_variable(scope, search_name):
    """Check if a variable is defined inside the same class or outside."""
    # TODO integrate this in the function that checks this.
    instance = scope.get_parent_scope()
    coming_from = search_name
    while coming_from is not None and not isinstance(coming_from, pr.Class):
        coming_from = coming_from.get_parent_scope()

    return isinstance(instance, er.Instance) and instance.base.base != coming_from
