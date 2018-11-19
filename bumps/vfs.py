"""
Redirect calls to open, etc. to a virtual file system.

Use this to mount a zip file as a file system, and then all subsequent calls
to chdir, open, etc. will reference files in the zip file instead of the disk.

This will only work for packages which do all their I/O in python, and not
those which use direct calls to the C library, so for example, it will not
work with h5py. XML parsing from a zip file is also unlikely to work since
expat uses the C library directly for parsing.

Usage::

    # Do this before importing any other modules!  It sets up hooks for
    # redirecting filesystem access even if the module imports the symbol
    # directly as "from os import getcwd".
    import vfs
    vfs.vfs_init()
    ...
    with vfs.ZipFS('data.zip'):
        data = np.loadtxt('file1.dat')

Filesystems available:

* :class:`RealFS` - uses the builtin python functions to access the O/S.

* :class:`ZipFS` - opens a zip file as the filesystem root.

Calls redirected::

    __builtin__.open (python 2 only)
    builtins.open (python 2 and 3)
    os.chdir
    os.getcwd
    os.listdir
    os.path.exists
    os.path.isfile
    os.path.isdir
    os.path.abspath
    os.path.realpath

You can also use the file systems directly without using the :func:`vfs_init`
hook or the with statement.  Just call `fs.chdir`, etc. on the file system
object.

*file* in python 2.x is a type as well as constructor, so a simple redirect
to a replacement constructor will not work. Don't try to support it since
it is gone in python 3.

Works with numpy.loadtxt and pandas parsers.

For pandas, either need to specify *engine="python"* or pass an open call
to the reader; if you just pass a filename, then it will try opening it
with the libc open function and fail.  Could potentially monkeypatch
pandas to pre-open the file.
"""
from __future__ import print_function

import sys
import os
import os.path
import builtins
import io
from functools import wraps

try:
    from __builtin__ import open as _py2_open
except ImportError:
    _py2_open = None

try:
    from pathlib import PurePath
except ImportError:
    class PurePath:
        pass

# TODO: restructure according to pathlib interface
# Looking at pathlib, it already provides methods close to what we implement
# for our virtual file system.  By following the pathlib interface directly,
# then any code that is set up to use pathlib can use our virtual file systems
# without change (in particular, without monkeypatching python builtins).

# for functions that work for read-only filesystems, use *fn
# for functions implemented as python, use -fn
#
# os functions
#   *chdir, *getcwd, *listdir, rmdir, mkdir, chroot
#   *open, *stat, *access, rename, link, unlink, remove chmod, chown, chflags
#   *getcwdb, *getcwdu  # bytes getcwd (py3), unicode getcwd (py2)
#   *scandir
#   -makedirs   # uses exists, split, mkdir
#   -removedirs # uses split, rmdir
#   -walk       # uses islink, join, isdir, listdir
#   -glob.glob  # uses lexists, isdir, join, scandir

# symbolic link functions
#   *readlink, *lstat, symlink, lchflags, lchmod, lchown
#   *os.path.lexists

# os.path functions
#   *exists, *isfile, *isdir
#   *get[acm]time, *getsize
#   samefile    # uses stat, samestat
#   samestat    # uses filestat.dev and filestat.inode; compares device/inode
#   -isabs      # uses str; returns s.startswith('/')
#   -normpath   # uses str; pure path manipulation
#   -abspath    # uses isabs, normpath, getcwd/getcwdu
#   -realpath   # uses isabs, split, join, islink, readlink
#   -renames    # uses exists, split, rename, makedirs, removedirs
#   -walk       # uses listdir, lstat;  deprecated in favour of os.walk

# os/os.path constants
#   curdir, pardir, sep, pathsep, defpath, extsep, altsep, linesep

# file descriptor operations in os
#   fchdir, fchmod, fchown, fdopen, close, fstat, fstatvfs, fpathconf,
#   lseek, read, dup, dup2, errno, error, closerange, isatty, openpty,
#   mknod

_open = builtins.open
_chdir = os.chdir
_getcwd = os.getcwd
_exists = os.path.exists
_isfile = os.path.isfile
_isdir = os.path.isdir
_listdir = os.listdir

# TODO: maybe use builtin versions?
_abspath = os.path.abspath
_realpath = os.path.realpath

class RealFS(object):
    def __enter__(self):
        pushfs(self)

    def __exit__(self, *args, **kw):
        popfs()

    def open(self, *args, **kw):
        return _open(*args, **kw)

    def py2_open(self, *args, **kw):
        return _py2_open(*args, **kw)

    def getcwd(self):
        return _getcwd()

    def chdir(self, path):
        return _chdir(path)

    def listdir(self, path=None):
        return _listdir(path) if path is not None else _listdir()

    def abspath(self, path):
        return _abspath(path)

    def realpath(self, path):
        return _realpath(path)

    def isfile(self, path):
        return _isfile(path)

    def isdir(self, path):
        return _isdir(path)

    def exists(self, path):
        return _exists(path)


class ZipFS(object):
    """
    Opens a zip file as the root file system.
    """
    def __init__(self, path):
        import zipfile
        # TODO: can we open a zip within a zip?
        # Apparently yes, but only if we read the file into a byte stream and
        # then work from that file.  See the following stackoverflow answer:
        #    https://stackoverflow.com/questions/12025469/how-to-read-from-a-zip-file-within-zip-file-in-python
        self._path = _realpath(path)
        self._wd = "/"
        self._zip = zipfile.ZipFile(path)

    def __enter__(self):
        pushfs(self)
        return self._zip

    def __exit__(self, *args, **kw):
        popfs()

    def open(self, file, mode="r", buffering=-1, encoding=None,
             errors=None, newline=None, **kw):
        # abspath handles pathlib
        # Note: python 3 zipfile only supports mode rb; to get unicode
        # decoding, need to wrap the binary stream in a text I/O wrapper.
        zipmode = 'r'
        with RealFS():
            fd = self._zip.open(self.abspath(file)[1:], mode=zipmode)
        if 'b' in mode:
            return fd
        else:
            return io.TextIOWrapper(fd, encoding=encoding, errors=errors,
                newline=newline)

    def py2_open(self, name, mode="r", buffering=-1):
        # abspath handles pathlib
        # Note: python 2 zipfile supports modes r, rU, and U, but not rb
        zipmode = 'r' if mode == 'rb' else mode
        with RealFS():
            fd = self._zip.open(self.abspath(name)[1:], mode=zipmode)
        return fd

    def chdir(self, path):
        # abspath handles pathlib
        if self.isdir(path):
            self._wd = self.abspath(path) + "/"

    def _iter_dir(self, path=None):
        # TODO: turn this into a scandir interface
        # abspath handles pathlib
        path = self._wd if path is None else self.abspath(path)
        path = path[1:]
        n = len(path)
        seen = {}
        for f in self._zip.filelist:
            if not f.filename.startswith(path):
                # it is not part of the tree
                continue
            parts = f.filename.split('/')
            if len(parts) == 1:
                # it is a leaf so report it
                yield parts[0]
            elif parts[0] not in seen:
                # it is a directory, so only report it if it has not already
                # been reported
                seen.add(parts[0])
                yield parts[0]

    def listdir(self, path=None):
        # abspath handles pathlib
        return [f for f in self._iter_dir(path)]

    def abspath(self, path):
        if isinstance(path, PurePath):
            path = path.as_posix()
        if hasattr(path, 'decode'): # CRUFT: python 2
            path= path.decode()
        if path[0] != '/':
            path = '/'.join((self._wd[:-1], path))
        return os.path.normpath(path)

    def realpath(self, filename):
        # abspath handles pathlib
        return os.path.join(self._path, self.abspath(filename))

    def isfile(self, path):
        # abspath handles pathlib
        path = self.abspath(path)[1:]
        return any(path == f.filename for f in self._zip.filelist)

    def isdir(self, s):
        # abspath handles pathlib
        path = self.abspath(s)[1:] + "/"
        for f in self._zip.filelist:
            if f.filename.startswith(path):
                return True
        return False

    def exists(self, path):
        # abspath handles pathlib
        return self.isfile(path) or self.isdir(path)

# These will be initialized in vfs_init
FS = None  # type: RealFS
FS_STACK = None  # type: List[RealFS]
def pushfs(fs):
    global FS
    FS_STACK.append(FS)
    FS = fs

def popfs():
    global FS
    FS = FS_STACK.pop()

@wraps(_open)
def fs_open(*args, **kw):
    return FS.open(*args, **kw)

@wraps(_open)
def fs_py2_open(*args, **kw):
    return FS.py2_open(*args, **kw)

@wraps(_chdir)
def fs_chdir(*args, **kw):
    return FS.chdir(*args, **kw)

@wraps(_getcwd)
def fs_getcwd(*args, **kw):
    return FS.getcwd(*args, **kw)

@wraps(_listdir)
def fs_listdir(*args, **kw):
    return FS.listdir(*args, **kw)

@wraps(_exists)
def fs_exists(*args, **kw):
    return FS.exists(*args)

@wraps(_isfile)
def fs_isfile(*args, **kw):
    return FS.isfile(*args, **kw)

@wraps(_isdir)
def fs_isdir(*args, **kw):
    return FS.isdir(*args, **kw)

@wraps(_abspath)
def fs_abspath(*args, **kw):
    return FS.abspath(*args, **kw)

@wraps(_realpath)
def fs_realpath(*args, **kw):
    return FS.realpath(*args, **kw)

def vfs_init():
    """
    Call this very early in your program so that various filesystem functions
    will be redirected even if they are expressed as "from module import fn"
    """
    global FS, FS_STACK
    FS = RealFS()
    FS_STACK = []

    if _py2_open is not None:
        import __builtin__
        __builtin__.open = fs_py2_open

    builtins.open = fs_open
    io.open = fs_open
    os.chdir = fs_chdir
    os.getcwd = fs_getcwd
    os.listdir = fs_listdir
    os.path.abspath = fs_abspath
    os.path.realpath = fs_realpath
    os.path.exists = fs_exists
    os.path.isfile= fs_isfile
    os.path.isdir = fs_isdir

    try:
        import nt, ntpath
        nt.chdir = fs_chdir
        nt.listdir = fs_listdir
        nt.getcwd = fs_getcwd
        ntpath.abspath = fs_abspath
        ntpath.realpath = fs_realpath
        ntpath.isfile = fs_isfile
        ntpath.isdir = fs_isdir
    except ImportError:
        pass

    try:
        import posix, posixpath
        posix.chdir = fs_chdir
        posix.listdir = fs_listdir
        posix.getcwd = fs_getcwd
        posixpath.abspath = fs_abspath
        posixpath.realpath = fs_realpath
        posixpath.isfile = fs_isfile
        posixpath.isdir = fs_isdir
    except ImportError:
        pass

    # Pathlib may be imported really early.  Make sure it sees the vfs.
    # TODO: reload may fail isinstance tests --- monkeypatch instead?
    try:
        import pathlib
        from importlib import reload
        reload(pathlib)
    except ImportError:
        pass
