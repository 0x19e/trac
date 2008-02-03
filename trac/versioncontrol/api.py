# -*- coding: utf-8 -*-
#
# Copyright (C) 2005 Edgewall Software
# Copyright (C) 2005 Christopher Lenz <cmlenz@gmx.de>
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution. The terms
# are also available at http://trac.edgewall.org/wiki/TracLicense.
#
# This software consists of voluntary contributions made by many
# individuals. For the exact contribution history, see the revision
# history and logs, available at http://trac.edgewall.org/log/.
#
# Author: Christopher Lenz <cmlenz@gmx.de>

import os.path

try:
    import threading
except ImportError:
    import dummy_threading as threading
    threading._get_ident = lambda: 0

from trac.config import Option
from trac.core import *
from trac.perm import PermissionError
from trac.resource import IResourceManager, ResourceSystem, ResourceNotFound
from trac.util.text import to_unicode
from trac.util.translation import _
from trac.web.api import IRequestFilter


class IRepositoryConnector(Interface):
    """Provide support for a specific version control system."""

    def get_supported_types():
        """Return the types of version control systems that are supported.

        Yields `(repotype, priority)` pairs, where `repotype` is used to
        match against the configured `[trac] repository_type` value in TracIni.
        
        If multiple provider match a given type, the `priority` is used to
        choose between them (highest number is highest priority).
        """

    def get_repository(repos_type, repos_dir, options):
        """Return a Repository instance for the given repository type and dir.
        """

class IRepositoryProvider(Interface):
    """Provide known named instances of Repository."""

    def get_repositories():
        """Generate repository information for known repositories.
        
        Repository information is a key,value pair, where the value is 
        a dictionary which must contain at the very least one of the following 
        entries:
         - `'dir'`: the repository directory which can be used by the 
                    connector to create a `Repository` instance
         - `'alias'`: if set, it is the name of another repository.
        Optional entries:
         - `'type'`: the type of the repository (if not given, the default
                     repository type will be used)
        """


class RepositoryManager(Component):
    """Component registering the supported version control systems,

    It provides easy access to the configured implementation.
    """

    implements(IRequestFilter, IResourceManager, IRepositoryProvider)

    connectors = ExtensionPoint(IRepositoryConnector)
    providers = ExtensionPoint(IRepositoryProvider)

    repository_type = Option('trac', 'repository_type', 'svn',
        """Default repository connector type. (''since 0.10'')""")
    repository_dir = Option('trac', 'repository_dir', '',
        """Path to the default repository. This can also be a relative path
        (''since 0.11'').""")

    def __init__(self):
        self._cache = {}
        self._lock = threading.Lock()
        self._connectors = None
        self._all_repositories = None

    # IRequestFilter methods

    def pre_process_request(self, req, handler):
        from trac.web.chrome import Chrome, add_warning
        if handler is not Chrome(self.env):
            try:
                # FIXME: only sync the default repository - this is bogus
                self.get_repository('', req.authname).sync()
            except TracError, e:
                add_warning(req, _("Can't synchronize with the repository "
                              "(%(error)s)", error=e.message))
        return handler

    def post_process_request(self, req, template, content_type):
        return (template, content_type)

    # IResourceManager methods

    # Note: with multiple repository support, the repository name becomes
    #       part of the 'id', which becomes a `(reponame, rev or path)` pair.

    def get_resource_realms(self):
        yield 'changeset'
        yield 'source'

    def get_resource_description(self, resource, format=None, **kwargs):
        reponame, id = resource.id
        if resource.realm == 'changeset':
            if reponame:
                return _("Changeset %(rev)s in %(repo)s", rev=id, repo=reponame)
            else:
                return _("Changeset %(rev)s", rev=id)
        elif resource.realm == 'source':
            version = in_repo = ''
            if format == 'summary':
                repos = resource.env.get_repository(reponame)
                node = repos.get_node(resource.id, resource.version)
                if node.isdir:
                    kind = _("Directory")
                elif node.isfile:
                    kind = _("File")
                if resource.version:
                    version = _("at version %(rev)s", rev=resource.version)
            else:
                kind = _("Path")
                if resource.version:
                    version = '@%s' % resource.version
            if reponame:
                in_repo = _("in %(repo)s", repo=reponame)
            return ''.join(kind, ' ', resource.id, version, in_repo)

    # IRepositoryProvider methods

    def get_repositories(self):
        """Retrieve repositories specified in TracIni.
        
        The `[repositories]` section can be used to specify a list
        of repositories.
        """
        repositories = self.config['repositories']
        reponames = {}
        # first pass to gather the <name>.dir entries
        for option in repositories:
            if option.endswith('.dir'):
                reponames[option[:-4]] = {}
        # second pass to gather the <name>.<detail> entries or <alias> ones
        for option in repositories:
            if '.' in option:
                dotindex = option.rindex('.')
                name, detail = option[:dotindex], option[dotindex+1:]
                if name in reponames:
                    reponames[name][detail] = repositories.get(option)
                else: # alias?
                    alias = repositories.get(option)
                    if alias in reponames:
                        reponames[option] = {'alias': alias}
        # eventually add pre-0.12 default repository
        if '' not in reponames:
            reponames[''] = {'dir': self.repository_dir}

        for reponame, info in reponames.iteritems():
            yield (reponame, info)

    # Public API methods

    def get_repository(self, reponame, authname):
        """Retrieve the appropriate Repository for the given name

           :param reponame: the key for specifying the repository.
                            If no name is given, take the the default 
                            repository 
           :param authname: deprecated (use fine grained permissions)
           :return: if no corresponding repository was defined, 
                    simply return `None`.
        """
        repoinfo = self.get_all_repositories().get(reponame, {})
        if repoinfo and 'alias' in repoinfo:
            repoinfo = self.get_all_repositories().get(repoinfo['alias'])
        if repoinfo:
            rdir = repoinfo.get('dir')
            rtype = repoinfo.get('type', self.repository_type)
            if not rdir:
                return None
        elif reponame:
            return None
        else:
            reponame = '' # normalize the name for the default repository
            rdir, rtype = self.repository_dir, self.repository_type

        # get a Repository for the reponame (use a thread-level cache)
        db = self.env.get_db_cnx() # prevent possible deadlock, see #4465
        try:
            self._lock.acquire()
            tid = threading._get_ident()
            if tid in self._cache:
                repositories = self._cache[tid]
            else:
                repositories = self._cache[tid] = {}
            repos = repositories.get(reponame)
            if not repos:
                if not os.path.isabs(rdir):
                    rdir = os.path.join(self.env.path, rdir)
                connector = self._get_connector(rtype)
                repos = connector.get_repository(rtype, rdir, repoinfo)
                repos.reponame = reponame
                repositories[reponame] = repos
            return repos
        finally:
            self._lock.release()

    def get_repository_by_path(self, path, authname):
        """Retrieve a matching Repository for the given path.
        
        :param path: the eventually scoped repository-scoped path
        :return: a `(reponame, repos, path)` triple, where `path` is 
                 the remaining part of `path` once the `reponame` has
                 been truncated, if needed.
        """
        matches = []
        path = path and path.strip('/')+'/' or '/'
        for reponame in self.get_all_repositories().keys():
            stripped_reponame = reponame.strip('/')+'/'
            if path.startswith(stripped_reponame):
                matches.append((len(stripped_reponame), reponame))
        if matches:
            matches.sort()
            length, reponame = matches[-1]
            path = path[length:]
        else:
            reponame = ''
        return (reponame, self.get_repository(reponame, authname), path or '/')

    def get_default_repository(self, context):
        """Recover the appropriatet repository from the current context.

        Lookup the closest source or changeset resource in the context 
        hierarchy and return the name of its associated repository.
        """
        while context:
            if context.resource.realm in ('source', 'changeset'):
                return context.resource.id[0]
            context = context.parent

    def get_all_repositories(self):
        """Return a dictionary of repository information, indexed by name."""
        if not self._all_repositories:
            self._all_repositories = {}
            for provider in self.providers:
                for reponame, info in provider.get_repositories():
                    if reponame in self._all_repositories:
                        self.log.warn("Discarding duplicate repository '%s'",
                                      reponame)
                    else:
                        self._all_repositories[reponame] = info
        return self._all_repositories

    def shutdown(self, tid=None):
        if tid:
            assert tid == threading._get_ident()
            try:
                self._lock.acquire()
                repositories = self._cache.pop(tid, {})
                for reponame, repos in repositories.iteritems():
                    repos.close()
            finally:
                self._lock.release()
        
    # private methods

    def _get_connector(self, rtype):
        """Retrieve the appropriate connector for the given repository type.
        
        Note that the self._lock must be held when calling this method.
        """
        if self._connectors is None:
            # build an environment-level cache for the preferred connectors
            self._connectors = {}
            prioritize = {}
            for connector in self.connectors:
                for type_, prio in connector.get_supported_types():
                    best = prioritize.setdefault(type_, [(None, 0)])
                    if prio > best[0][1]:
                        best[0] = (connector, prio)
            for type_, best in prioritize.iteritems():
                    self._connectors[type_] = best[0][0]
        connector = self._connectors[rtype]
        if not connector:
            raise TracError(
                    _('Unsupported version control system "%(name)s". '
                      'Check that the Python support libraries for '
                      '"%(name)s" are correctly installed.',
                      name=self.repository_type))
        return connector


class NoSuchChangeset(ResourceNotFound):
    def __init__(self, rev):
        ResourceNotFound.__init__(self,
                                  _('No changeset %(rev)s in the repository',
                                    rev=rev),
                                  _('No such changeset'))

class NoSuchNode(ResourceNotFound):
    def __init__(self, path, rev, msg=None):
        ResourceNotFound.__init__(self, "%sNo node %s at revision %s" %
                                  ((msg and '%s: ' % msg) or '', path, rev),
                                  _('No such node'))

class Repository(object):
    """Base class for a repository provided by a version control system."""

    def __init__(self, name, authz, log):
        self.name = name
        self.authz = authz or Authorizer()
        self.log = log
        self.reponame = name # overriden by the reponame key used to create it

    def close(self):
        """Close the connection to the repository."""
        raise NotImplementedError

    def clear(self, youngest_rev=None):
        """Clear any data that may have been cached in instance properties.

        `youngest_rev` can be specified as a way to force the value
        of the `youngest_rev` property (''will change in 0.12'').
        """
        pass

    def sync(self, rev_callback=None):
        """Perform a sync of the repository cache, if relevant.
        
        If given, `rev_callback` must be a callable taking a `rev` parameter.
        The backend will call this function for each `rev` it decided to
        synchronize, once the synchronization changes are committed to the 
        cache.
        """
        pass

    def sync_changeset(self, rev):
        """Resync the repository cache for the given `rev`, if relevant."""
        raise NotImplementedError

    def get_quickjump_entries(self, rev):
        """Generate a list of interesting places in the repository.

        `rev` might be used to restrict the list of available locations,
        but in general it's best to produce all known locations.

        The generated results must be of the form (category, name, path, rev).
        """
        return []
    
    def get_changeset(self, rev):
        """Retrieve a Changeset corresponding to the  given revision `rev`."""
        raise NotImplementedError

    def get_changesets(self, start, stop):
        """Generate Changeset belonging to the given time period (start, stop).
        """
        rev = self.youngest_rev
        while rev:
            if self.authz.has_permission_for_changeset(rev):
                chgset = self.get_changeset(rev)
                if chgset.date < start:
                    return
                if chgset.date < stop:
                    yield chgset
            rev = self.previous_rev(rev)

    def has_node(self, path, rev=None):
        """Tell if there's a node at the specified (path,rev) combination.

        When `rev` is `None`, the latest revision is implied.
        """
        try:
            self.get_node(path, rev)
            return True
        except TracError:
            return False        
    
    def get_node(self, path, rev=None):
        """Retrieve a Node from the repository at the given path.

        A Node represents a directory or a file at a given revision in the
        repository.
        If the `rev` parameter is specified, the Node corresponding to that
        revision is returned, otherwise the Node corresponding to the youngest
        revision is returned.
        """
        raise NotImplementedError

    def get_oldest_rev(self):
        """Return the oldest revision stored in the repository."""
        raise NotImplementedError
    oldest_rev = property(lambda x: x.get_oldest_rev())

    def get_youngest_rev(self):
        """Return the youngest revision in the repository."""
        raise NotImplementedError
    youngest_rev = property(lambda x: x.get_youngest_rev())

    def previous_rev(self, rev):
        """Return the revision immediately preceding the specified revision."""
        raise NotImplementedError

    def next_rev(self, rev, path=''):
        """Return the revision immediately following the specified revision."""
        raise NotImplementedError

    def rev_older_than(self, rev1, rev2):
        """Provides a total order over revisions.
        
        Return `True` if `rev1` is older than `rev2`, i.e. if `rev1`
        comes before `rev2` in the revision sequence.
        """
        raise NotImplementedError

    def get_youngest_rev_in_cache(self, db):
        """Return the youngest revision currently cached.
        
        The way revisions are sequenced is version control specific.
        By default, one assumes that the revisions are sequenced in time
        (... which is ''not'' correct for most VCS, including Subversion).

        (Deprecated, will not be used anymore in Trac 0.12)
        """
        cursor = db.cursor()
        cursor.execute("SELECT rev FROM revision ORDER BY time DESC LIMIT 1")
        row = cursor.fetchone()
        return row and row[0] or None

    def get_path_history(self, path, rev=None, limit=None):
        """Retrieve all the revisions containing this path

        If given, `rev` is used as a starting point (i.e. no revision
        ''newer'' than `rev` should be returned).
        The result format should be the same as the one of Node.get_history()
        """
        raise NotImplementedError

    def normalize_path(self, path):
        """Return a canonical representation of path in the repos."""
        raise NotImplementedError

    def normalize_rev(self, rev):
        """Return a canonical representation of a revision.

        It's up to the backend to decide which string values of `rev` 
        (usually provided by the user) should be accepted, and how they 
        should be normalized. Some backends may for instance want to match
        against known tags or branch names.
        
        In addition, if `rev` is `None` or '', the youngest revision should
        be returned.
        """
        raise NotImplementedError

    def short_rev(self, rev):
        """Return a compact representation of a revision in the repos."""
        return self.normalize_rev(rev)
        
    def get_changes(self, old_path, old_rev, new_path, new_rev,
                    ignore_ancestry=1):
        """Generates changes corresponding to generalized diffs.
        
        Generator that yields change tuples (old_node, new_node, kind, change)
        for each node change between the two arbitrary (path,rev) pairs.

        The old_node is assumed to be None when the change is an ADD,
        the new_node is assumed to be None when the change is a DELETE.
        """
        raise NotImplementedError


class Node(object):
    """Represents a directory or file in the repository at a given revision."""

    DIRECTORY = "dir"
    FILE = "file"

    # created_path and created_rev properties refer to the Node "creation"
    # in the Subversion meaning of a Node in a versioned tree (see #3340).
    #
    # Those properties must be set by subclasses.
    #
    created_rev = None   
    created_path = None

    def __init__(self, path, rev, kind):
        assert kind in (Node.DIRECTORY, Node.FILE), \
               "Unknown node kind %s" % kind
        self.path = to_unicode(path)
        self.rev = rev
        self.kind = kind

    def get_content(self):
        """Return a stream for reading the content of the node.

        This method will return `None` for directories.
        The returned object must support a `read([len])` method.
        """
        raise NotImplementedError

    def get_entries(self):
        """Generator that yields the immediate child entries of a directory.

        The entries are returned in no particular order.
        If the node is a file, this method returns `None`.
        """
        raise NotImplementedError

    def get_history(self, limit=None):
        """Provide backward history for this Node.
        
        Generator that yields `(path, rev, chg)` tuples, one for each revision
        in which the node was changed. This generator will follow copies and
        moves of a node (if the underlying version control system supports
        that), which will be indicated by the first element of the tuple
        (i.e. the path) changing.
        Starts with an entry for the current revision.
        """
        raise NotImplementedError

    def get_previous(self):
        """Return the change event corresponding to the previous revision.

        This returns a `(path, rev, chg)` tuple.
        """
        skip = True
        for p in self.get_history(2):
            if skip:
                skip = False
            else:
                return p

    def get_annotations(self):
        """Provide detailed backward history for the content of this Node.

        Retrieve an array of revisions, one `rev` for each line of content
        for that node.
        Only expected to work on (text) FILE nodes, of course.
        """
        raise NotImplementedError

    def get_properties(self):
        """Returns the properties (meta-data) of the node, as a dictionary.

        The set of properties depends on the version control system.
        """
        raise NotImplementedError

    def get_content_length(self):
        """The length in bytes of the content.

        Will be `None` for a directory.
        """
        raise NotImplementedError
    content_length = property(lambda x: x.get_content_length())

    def get_content_type(self):
        """The MIME type corresponding to the content, if known.

        Will be `None` for a directory.
        """
        raise NotImplementedError
    content_type = property(lambda x: x.get_content_type())

    def get_name(self):
        return self.path.split('/')[-1]
    name = property(lambda x: x.get_name())

    def get_last_modified(self):
        raise NotImplementedError
    last_modified = property(lambda x: x.get_last_modified())

    isdir = property(lambda x: x.kind == Node.DIRECTORY)
    isfile = property(lambda x: x.kind == Node.FILE)


class Changeset(object):
    """Represents a set of changes committed at once in a repository."""

    ADD = 'add'
    COPY = 'copy'
    DELETE = 'delete'
    EDIT = 'edit'
    MOVE = 'move'

    # change types which can have diff associated to them
    DIFF_CHANGES = (EDIT, COPY, MOVE) # MERGE
    OTHER_CHANGES = (ADD, DELETE)
    ALL_CHANGES = DIFF_CHANGES + OTHER_CHANGES

    def __init__(self, rev, message, author, date):
        self.rev = rev
        self.message = message or ''
        self.author = author or ''
        self.date = date
    
    def get_properties(self):
        """Returns the properties (meta-data) of the node, as a dictionary.

        The set of properties depends on the version control system.

        Warning: this used to yield 4-elements tuple (besides `name` and
        `text`, there were `wikiflag` and `htmlclass` values).
        This is now replaced by the usage of IPropertyRenderer (see #1601).
        """
        return []
        
    def get_changes(self):
        """Generator that produces a tuple for every change in the changeset

        The tuple will contain `(path, kind, change, base_path, base_rev)`,
        where `change` can be one of Changeset.ADD, Changeset.COPY,
        Changeset.DELETE, Changeset.EDIT or Changeset.MOVE,
        and `kind` is one of Node.FILE or Node.DIRECTORY.
        The `path` is the targeted path for the `change` (which is
        the ''deleted'' path  for a DELETE change).
        The `base_path` and `base_rev` are the source path and rev for the
        action (`None` and `-1` in the case of an ADD change).
        """
        raise NotImplementedError

    def get_uid(self):
        """Return a globally unique identifier for this changesets.

        Two changesets from different repositories can sometimes refer to
        the ''very same'' changesets (e.g. two different clones)
        """


class PermissionDenied(PermissionError):
    """Exception raised by an authorizer.

    This exception is raise if the user has insufficient permissions
    to view a specific part of the repository.
    """
    def __str__(self):
        return self.action


class Authorizer(object):
    """Controls the view access to parts of the repository.
    
    Base class for authorizers that are responsible to granting or denying
    access to view certain parts of a repository.
    """

    def assert_permission(self, path):
        if not self.has_permission(path):
            raise PermissionDenied(_('Insufficient permissions to access '
                                     '%(path)s', path=path))

    def assert_permission_for_changeset(self, rev):
        if not self.has_permission_for_changeset(rev):
            raise PermissionDenied(_('Insufficient permissions to access '
                                     'changeset %(id)s', id=rev))

    def has_permission(self, path):
        return True

    def has_permission_for_changeset(self, rev):
        return True
