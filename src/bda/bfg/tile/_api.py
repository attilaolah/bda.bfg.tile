import os
import cgi
from webob import Response
from webob.exc import HTTPFound
from zope.interface import (
    Interface, 
    Attribute, 
    implements,
    directlyProvides,
)
from zope.component import ComponentLookupError
from repoze.bfg.interfaces import (
    IRequest,
    IResponseFactory,
    IAuthenticationPolicy,
    IAuthorizationPolicy,
    IDebugLogger,
)
from repoze.bfg.settings import get_settings
from repoze.bfg.configuration import decorate_view
from repoze.bfg.exceptions import Forbidden
from repoze.bfg.threadlocal import get_current_registry
from repoze.bfg.path import caller_package
from repoze.bfg.renderers import template_renderer_factory
from repoze.bfg.chameleon_zpt import ZPTTemplateRenderer

class ITile(Interface):
    """Renders some HTML snippet.
    """
    
    name = Attribute(u"The name und which this tile is registered.")
    show = Attribute(u"Flag wether to render the tile.")
    
    def __call__(model, request):
        """Renders the tile.
        
        It's intended to work this way: First it calls its own prepare method, 
        then it checks its own show attribute. If this returns True it renders 
        the template in the context of the ITile implementing class instance.  
        """
        
    def prepare():
        """Prepares the tile.
        
        I.e. fetch data to display ... 
        """
    
def _update_kw(**kw):
    if not ('request' in kw and 'model' in kw):
        raise ValueError, "Expected kwargs missing: model, request."
    kw.update({'tile': TileRenderer(kw['model'], kw['request'])})    
    return kw

def _redirect(kw):
    if kw['request'].environ.get('redirect'):
        return True
    return False
    
def render_template(path, **kw):
    kw = _update_kw(**kw)
    if _redirect(kw):
        return u''
    if not (':' in path or os.path.isabs(path)): 
        raise ValueError, 'Relative path not supported: %s' % path
    renderer = template_renderer_factory(path, ZPTTemplateRenderer)
    return renderer(kw, {})    
    
def render_template_to_response(path, **kw):
    kw = _update_kw(**kw)
    kw['request'].environ['redirect'] = None
    renderer = template_renderer_factory(path, ZPTTemplateRenderer)
    result = renderer(kw, {})
    if _redirect(kw):
        return HTTPFound(location=kw['request'].environ['redirect'])
    response_factory = kw['request'].registry.queryUtility(IResponseFactory,
                                                           default=Response)
    return response_factory(result)

def render_to_response(request, result):
    if _redirect(kw={'request': request}):
        return HTTPFound(location=request.environ['redirect'])
    response_factory = request.registry.queryUtility(IResponseFactory,
                                                     default=Response)
    return response_factory(result)

def render_tile(model, request, name):
    """renders a tile. Intended usage is in application code.
    
    ``model``
        application model aka context
        
    ``request``
        the current request
        
    ``name`` 
        name of the requested tile
    """
    try:
        tile = request.registry.getMultiAdapter((model, request), ITile, name=name)
    except ComponentLookupError, e:
        return u"Tile with name '%s' not found:<br /><pre>%s</pre>" % \
               (name, cgi.escape(str(e)))
    return tile

class TileRenderer(object):
    """Renders a tile. Intended usage is as instance in template code."""
    
    def __init__(self, model, request):
        self.model, self.request = model, request
    
    def __call__(self, name):
        return render_tile(self.model, self.request, name)
    
class Tile(object):
    implements(ITile)
    
    def __init__(self, path, attribute, name):
        self.name = name
        self.path = path
        self.attribute = attribute

    def __call__(self, model, request):
        self.model = model
        self.request = request
        self.prepare() # TODO: discuss if needed. i think yes (jens)
        if not self.show:
            return u''
        if self.path:
            return render_template(self.path, request=request,
                                       model=model, context=self)
        renderer = getattr(self, self.attribute)
        result = renderer()
        return result
    
    @property
    def show(self): 
        return True
    
    def prepare(self): 
        pass
    
    def render(self):
        return u''
    
    def redirect(self, url):
        # why do we need a redirect in a tile!?
        # a.: a tile is not always rendered to the response, form tiles i.e.
        # might perform redirection.
        self.request.environ['redirect'] = url
    
    @property
    def nodeurl(self):
        relpath = [p for p in self.model.path if p is not None]
        return '/'.join([self.request.application_url] + relpath)
    
def _secure_tile(tile, permission, authn_policy, authz_policy, strict):
    """wraps tile and does security checks.
    """
    wrapped_tile = tile
    if not authn_policy and not authz_policy:
        return tile
    def _secured_tile(context, request):
        principals = authn_policy.effective_principals(request)
        if authz_policy.permits(context, principals, permission):
            try:
                return tile(context, request)
            except Exception, e:
                raise
        msg = getattr(request, 'authdebug_message',
                      'Unauthorized: tile %s failed permission check' % tile)
        if strict:
            raise Forbidden(msg)
        settings = get_settings()
        if settings.get('debug_authorization', False):
            logger = IDebugLogger()
            logger.debug(msg)
        return u''
    _secured_tile.__call_permissive__ = tile
    def _permitted(context, request):
        principals = authn_policy.effective_principals(request)
        return authz_policy.permits(context, principals, permission)
    _secured_tile.__permitted__ = _permitted
    wrapped_tile = _secured_tile
    decorate_view(wrapped_tile, tile)
    return wrapped_tile

# Registration
def registerTile(name, path=None, attribute='render',
                 interface=Interface, class_=Tile, 
                 permission='view', strict=True, _level=2):
    """registers a tile.
    
    ``name``
        identifier of the tile (for later lookup).
    
    ``path``
        either relative path to the template or absolute path or path prefixed
        by the absolute package name delimeted by ':'. If ``path`` is used
        ``attribute`` is ignored. 
        
    ``attribute``
        attribute on the given _class to be used to render the tile. Defaults to
        ``render``.
        
    ``interface`` 
        Interface or Class of the bfg model the tile is registered for.
        
    ``class_``
        Class to be used to render the tile. usally ``bda.bfg.tile.Tile`` or a
        subclass of. Promises to implement ``bda.bfg.ITile``.
        
    ``permission`` 
        Enables security checking for this tile. Defaults to ``view``. If set to
        ``None`` security checks are disabled.
        
    ``strict``
        Wether to raise ``Forbidden`` or not. Defaults to ``True``. If set to 
        ``False`` the exception is consumed and an empty unicode string is 
        returned.

    ``_level`` 
        is a bit special to make doctests pass the magic path-detection.
        you must never touch it in application code.
    """ 
    if path and not (':' in path or os.path.isabs(path)): 
        path = '%s:%s' % (caller_package(_level).__name__, path)
    tile = class_(path, attribute, name)
    registry = get_current_registry()
    if permission is not None:
        authn_policy = registry.queryUtility(IAuthenticationPolicy)
        authz_policy = registry.queryUtility(IAuthorizationPolicy)    
        tile = _secure_tile(tile, permission, authn_policy, authz_policy, 
                            strict)
    registry.registerAdapter(tile, [interface, IRequest], ITile, name, 
                             event=False)
    
class tile(object):
    """Decorator to register classes and functions as tiles.
    """
    
    def __init__(self, name, path=None, attribute='render',
                 interface=Interface, permission='view',
                 strict=True, _level=2):
        """ see ``registerTile`` for details on the other parameters.
        """
        self.name = name
        if path and not (':' in path or os.path.isabs(path)): 
            path = '%s:%s' % (caller_package(_level).__name__, path)
        self.path = path
        self.attribute = attribute
        self.interface = interface
        self.permission = permission
        self.strict = strict

    def __call__(self, ob):
        registerTile(self.name,
                     path=self.path,
                     attribute=self.attribute,
                     interface=self.interface,
                     class_=ob,
                     permission=self.permission,
                     strict=self.strict)
        return ob
