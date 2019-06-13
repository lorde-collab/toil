# Copyright (C) 2015-2018 Regents of the University of California
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import, print_function
from future import standard_library
standard_library.install_aliases()
from builtins import map
from builtins import str
from builtins import range
from builtins import object
from abc import abstractmethod, ABCMeta
from collections import namedtuple, defaultdict
from contextlib import contextmanager
from fcntl import flock, LOCK_EX, LOCK_UN
from functools import partial
from hashlib import sha1
from threading import Thread, Semaphore, Event
from future.utils import with_metaclass
from six.moves.queue import Empty, Queue
import base64
import dill
import errno
import logging
import os
import shutil
import stat
import tempfile
import time
import uuid

from toil.lib.objects import abstractclassmethod
from toil.lib.humanize import bytes2human
from toil.common import cacheDirName, getDirSizeRecursively, getFileSystemSize
from toil.lib.bioio import makePublicDir
from toil.resource import ModuleDescriptor

logger = logging.getLogger(__name__)

class WriteWatchingStream(object):
    """
    A stream wrapping class that calls any functions passed to onWrite() with the number of bytes written for every write.
    
    Not seekable.
    """
    
    def __init__(self, backingStream):
        """
        Wrap the given backing stream.
        """
        
        self.backingStream = backingStream
        # We have no write listeners yet
        self.writeListeners = []
        
    def onWrite(self, listener):
        """
        Call the given listener with the number of bytes written on every write.
        """
        
        self.writeListeners.append(listener)
        
    # Implement the file API from https://docs.python.org/2.4/lib/bltin-file-objects.html
        
    def write(self, data):
        """
        Write the given data to the file.
        """
        
        # Do the write
        self.backingStream.write(data)
        
        for listener in self.writeListeners:
            # Send out notifications
            listener(len(data))
            
    def writelines(self, datas):
        """
        Write each string from the given iterable, without newlines.
        """
        
        for data in datas:
            self.write(data)
            
    def flush(self):
        """
        Flush the backing stream.
        """
        
        self.backingStream.flush()
        
    def close(self):
        """
        Close the backing stream.
        """
        
        self.backingStream.close()


class FileID(str):
    """
    A small wrapper around Python's builtin string class. It is used to represent a file's ID in the file store, and
    has a size attribute that is the file's size in bytes. This object is returned by importFile and writeGlobalFile.
    """

    def __new__(cls, fileStoreID, *args):
        return super(FileID, cls).__new__(cls, fileStoreID)

    def __init__(self, fileStoreID, size):
        # Don't pass an argument to parent class's __init__.
        # In Python 3 we can have super(FileID, self) hand us object's __init__ which chokes on any arguments.
        super(FileID, self).__init__()
        self.size = size

    @classmethod
    def forPath(cls, fileStoreID, filePath):
        return cls(fileStoreID, os.stat(filePath).st_size)


class DeferredFunction(namedtuple('DeferredFunction', 'function args kwargs name module')):
    """
    >>> df = DeferredFunction.create(defaultdict, None, {'x':1}, y=2)
    >>> df
    DeferredFunction(defaultdict, ...)
    >>> df.invoke() == defaultdict(None, x=1, y=2)
    True
    """
    @classmethod
    def create(cls, function, *args, **kwargs):
        """
        Capture the given callable and arguments as an instance of this class.

        :param callable function: The deferred action to take in the form of a function
        :param tuple args: Non-keyword arguments to the function
        :param dict kwargs: Keyword arguments to the function
        """
        # The general principle is to deserialize as late as possible, i.e. when the function is
        # to be invoked, as that will avoid redundantly deserializing deferred functions for
        # concurrently running jobs when the cache state is loaded from disk. By implication we
        # should serialize as early as possible. We need to serialize the function as well as its
        # arguments.
        return cls(*list(map(dill.dumps, (function, args, kwargs))),
                   name=function.__name__,
                   module=ModuleDescriptor.forModule(function.__module__).globalize())

    def invoke(self):
        """
        Invoke the captured function with the captured arguments.
        """
        logger.debug('Running deferred function %s.', self)
        self.module.makeLoadable()
        function, args, kwargs = list(map(dill.loads, (self.function, self.args, self.kwargs)))
        return function(*args, **kwargs)

    def __str__(self):
        return '%s(%s, ...)' % (self.__class__.__name__, self.name)

    __repr__ = __str__


class FileStore(with_metaclass(ABCMeta, object)):
    """
    Interface used to allow user code run by Toil to read and write files.
    
    Also provides the interface to other Toil facilities used by user code,
    including:
    
     * normal (non-real-time) logging
     * finding the correct temporary directory for scratch work
     * importing and exporting files into and out of the workflow
   
    Also provides the backend for implementing deferred functions, although the
    user-facing interface goes through the job object.

    Stores user files in the jobStore, but keeps them separate from actual
    jobs.

    May implement caching.

    Passed as argument to the :meth:`toil.job.Job.run` method.

    Access to files is only permitted inside the context manager provided by
    :meth:`toil.fileStore.FileStore.open`. 
    """
    # Variables used for syncing reads/writes
    _pendingFileWritesLock = Semaphore()
    _pendingFileWrites = set()
    _terminateEvent = Event()  # Used to signify crashes in threads

    def __init__(self, jobStore, jobGraph, localTempDir, inputBlockFn):
        self.jobStore = jobStore
        self.jobGraph = jobGraph
        self.localTempDir = os.path.abspath(localTempDir)
        self.workFlowDir = os.path.dirname(self.localTempDir)
        self.jobName = self.jobGraph.command.split()[1]
        self.inputBlockFn = inputBlockFn
        self.loggingMessages = []
        self.filesToDelete = set()
        self.jobsToDelete = set()

    @staticmethod
    def createFileStore(jobStore, jobGraph, localTempDir, inputBlockFn, caching):
        # Defer these imports until runtime, since these classes depend on us
        from toil.fileStore.cachingFileStore import CachingFileStore
        from toil.fileStore.nonCachingFileStore import NonCachingFileStore
        fileStoreCls = CachingFileStore if caching else NonCachingFileStore
        return fileStoreCls(jobStore, jobGraph, localTempDir, inputBlockFn)

    @abstractmethod
    @contextmanager
    def open(self, job):
        """
        The context manager used to conduct tasks prior-to, and after a job has
        been run. File operations are only permitted inside the context
        manager.

        :param toil.job.Job job: The job instance of the toil job to run.
        """
        raise NotImplementedError()

    # Functions related to temp files and directories
    def getLocalTempDir(self):
        """
        Get a new local temporary directory in which to write files that persist for the duration of
        the job.

        :return: The absolute path to a new local temporary directory. This directory will exist
                 for the duration of the job only, and is guaranteed to be deleted once the job
                 terminates, removing all files it contains recursively.
        :rtype: str
        """
        return os.path.abspath(tempfile.mkdtemp(prefix="t", dir=self.localTempDir))

    def getLocalTempFile(self):
        """
        Get a new local temporary file that will persist for the duration of the job.

        :return: The absolute path to a local temporary file. This file will exist for the
                 duration of the job only, and is guaranteed to be deleted once the job terminates.
        :rtype: str
        """
        handle, tmpFile = tempfile.mkstemp(prefix="tmp", suffix=".tmp", dir=self.localTempDir)
        os.close(handle)
        return os.path.abspath(tmpFile)

    def getLocalTempFileName(self):
        """
        Get a valid name for a new local file. Don't actually create a file at the path.

        :return: Path to valid file
        :rtype: str
        """
        # Create, and then delete a temp file. Creating will guarantee you a unique, unused
        # file name. There is a very, very, very low chance that another job will create the
        # same file name in the span of this one being deleted and then being used by the user.
        tempFile = self.getLocalTempFile()
        os.remove(tempFile)
        return tempFile

    # Functions related to reading, writing and removing files to/from the job store
    @abstractmethod
    def writeGlobalFile(self, localFileName, cleanup=False):
        """
        Takes a file (as a path) and uploads it to the job store.

        :param string localFileName: The path to the local file to upload.
        :param bool cleanup: if True then the copy of the global file will be deleted once the
               job and all its successors have completed running.  If not the global file must be
               deleted manually.
        :return: an ID that can be used to retrieve the file.
        :rtype: toil.fileStore.FileID
        """
        raise NotImplementedError()

    @contextmanager
    def writeGlobalFileStream(self, cleanup=False):
        """
        Similar to writeGlobalFile, but allows the writing of a stream to the job store.
        The yielded file handle does not need to and should not be closed explicitly.

        :param bool cleanup: is as in :func:`toil.fileStore.FileStore.writeGlobalFile`.
        :return: A context manager yielding a tuple of
                  1) a file handle which can be written to and
                  2) the toil.fileStore.FileID of the resulting file in the job store.
        """
        
        # TODO: Make this work with FileID
        with self.jobStore.writeFileStream(None if not cleanup else self.jobGraph.jobStoreID) as (backingStream, fileStoreID):
            # We have a string version of the file ID, and the backing stream.
            # We need to yield a stream the caller can write to, and a FileID
            # that accurately reflects the size of the data written to the
            # stream. We assume the stream is not seekable.
            
            # Make and keep a reference to the file ID, which is currently empty
            fileID = FileID(fileStoreID, 0)
            
            # Wrap the stream to increment the file ID's size for each byte written
            wrappedStream = WriteWatchingStream(backingStream)
            
            # When the stream is written to, count the bytes
            def handle(numBytes):
                fileID.size += numBytes 
            wrappedStream.onWrite(handle)
            
            yield wrappedStream, fileID

    @abstractmethod
    def readGlobalFile(self, fileStoreID, userPath=None, cache=True, mutable=False, symlink=False):
        """
        Makes the file associated with fileStoreID available locally. If mutable is True,
        then a copy of the file will be created locally so that the original is not modified
        and does not change the file for other jobs. If mutable is False, then a link can
        be created to the file, saving disk resources.

        If a user path is specified, it is used as the destination. If a user path isn't
        specified, the file is stored in the local temp directory with an encoded name.

        :param toil.fileStore.FileID fileStoreID: job store id for the file
        :param string userPath: a path to the name of file to which the global file will be copied
               or hard-linked (see below).
        :param bool cache: Described in :func:`toil.fileStore.CachingFileStore.readGlobalFile`
        :param bool mutable: Described in :func:`toil.fileStore.CachingFileStore.readGlobalFile`
        :return: An absolute path to a local, temporary copy of the file keyed by fileStoreID.
        :rtype: str
        """
        raise NotImplementedError()

    @abstractmethod
    def readGlobalFileStream(self, fileStoreID):
        """
        Similar to readGlobalFile, but allows a stream to be read from the job store. The yielded
        file handle does not need to and should not be closed explicitly.

        :return: a context manager yielding a file handle which can be read from.
        """
        raise NotImplementedError()

    @abstractmethod
    def deleteLocalFile(self, fileStoreID):
        """
        Deletes Local copies of files associated with the provided job store ID.

        :param str fileStoreID: File Store ID of the file to be deleted.
        """
        raise NotImplementedError()

    @abstractmethod
    def deleteGlobalFile(self, fileStoreID):
        """
        Deletes local files with the provided job store ID and then permanently deletes them from
        the job store. To ensure that the job can be restarted if necessary, the delete will not
        happen until after the job's run method has completed.

        :param fileStoreID: the job store ID of the file to be deleted.
        """
        raise NotImplementedError()

    # Functions used to read and write files directly between a source url and the job store.
    def importFile(self, srcUrl, sharedFileName=None):
        return self.jobStore.importFile(srcUrl, sharedFileName=sharedFileName)

    def exportFile(self, jobStoreFileID, dstUrl):
        raise NotImplementedError()

    # A utility method for accessing filenames
    def _resolveAbsoluteLocalPath(self, filePath):
        """
        Return the absolute path to filePath.  This is a wrapper for os.path.abspath because mac OS
        symlinks /tmp and /var (the most common places for a default tempdir) to /private/tmp and
        /private/var respectively.

        :param str filePath: The absolute or relative path to the file. If relative, it must be
               relative to the local temp working dir
        :return: Absolute path to key
        :rtype: str
        """
        if os.path.isabs(filePath):
            return os.path.abspath(filePath)
        else:
            return os.path.join(self.localTempDir, filePath)

    class _StateFile(object):
        """
        Utility class to read and write dill-ed state dictionaries from/to a file into a namespace.
        """
        def __init__(self, stateDict):
            assert isinstance(stateDict, dict)
            self.__dict__.update(stateDict)

        @abstractclassmethod
        @contextmanager
        def open(cls, outer=None):
            """
            This is a context manager that state file and reads it into an object that is returned
            to the user in the yield.

            :param outer: Instance of the calling class (to use outer methods).
            """
            raise NotImplementedError()

        @classmethod
        def _load(cls, fileName):
            """
            Load the state of the cache from the state file

            :param str fileName: Path to the cache state file.
            :return: An instance of the state as a namespace.
            :rtype: _StateFile
            """
            # Read the value from the cache state file then initialize and instance of
            # _CacheState with it.
            with open(fileName, 'rb') as fH:
                infoDict = dill.load(fH)
            return cls(infoDict)

        def write(self, fileName):
            """
            Write the current state into a temporary file then atomically rename it to the main
            state file.

            :param str fileName: Path to the state file.
            """
            with open(fileName + '.tmp', 'wb') as fH:
                # Based on answer by user "Mark" at:
                # http://stackoverflow.com/questions/2709800/how-to-pickle-yourself
                # We can't pickle nested classes. So we have to pickle the variables of the class
                # If we ever change this, we need to ensure it doesn't break FileID
                dill.dump(self.__dict__, fH)
            os.rename(fileName + '.tmp', fileName)

    # Methods related to the deferred function logic
    @abstractclassmethod
    def findAndHandleDeadJobs(cls, nodeInfo, batchSystemShutdown=False):
        """
        This function looks at the state of all jobs registered on the node and will handle them
        (clean up their presence on the node, and run any registered defer functions)

        :param nodeInfo: Information regarding the node required for identifying dead jobs.
        :param bool batchSystemShutdown: Is the batch system in the process of shutting down?
        """
        raise NotImplementedError()

    @abstractmethod
    def _registerDeferredFunction(self, deferredFunction):
        """
        Register the given deferred function with this job.

        :param DeferredFunction deferredFunction: the function to register
        """
        raise NotImplementedError()

    @staticmethod
    def _runDeferredFunctions(deferredFunctions):
        """
        Invoke the specified deferred functions and return a list of names of functions that
        raised an exception while being invoked.

        :param list[DeferredFunction] deferredFunctions: the DeferredFunctions to run
        :rtype: list[str]
        """
        failures = []
        for deferredFunction in deferredFunctions:
            try:
                deferredFunction.invoke()
            except:
                failures.append(deferredFunction.name)
                logger.exception('%s failed.', deferredFunction)
        return failures

    # Functions related to logging
    def logToMaster(self, text, level=logging.INFO):
        """
        Send a logging message to the leader. The message will also be \
        logged by the worker at the same level.

        :param text: The string to log.
        :param int level: The logging level.
        """
        logger.log(level=level, msg=("LOG-TO-MASTER: " + text))
        self.loggingMessages.append(dict(text=text, level=level))

    # Functions run after the completion of the job.
    @abstractmethod
    def _updateJobWhenDone(self):
        """
        Update the status of the job on the disk.
        """
        raise NotImplementedError()

    @abstractmethod
    def _blockFn(self):
        """
        Blocks while _updateJobWhenDone is running. This function is called by this job's
        successor to ensure that it does not begin modifying the job store until after this job has
        finished doing so.
        """
        raise NotImplementedError()

    # Utility function used to identify if a pid is still running on the node.
    @staticmethod
    def _pidExists(pid):
        """
        This will return True if the process associated with pid is still running on the machine.
        This is based on stackoverflow question 568271.

        :param int pid: ID of the process to check for
        :return: True/False
        :rtype: bool
        """
        assert pid > 0
        try:
            os.kill(pid, 0)
        except OSError as err:
            if err.errno == errno.ESRCH:
                # ESRCH == No such process
                return False
            else:
                raise
        else:
            return True

    @abstractclassmethod
    def shutdown(cls, dir_):
        """
        Shutdown the filestore on this node.

        This is intended to be called on batch system shutdown.

        :param dir_: The jeystone directory containing the required information for fixing the state
               of failed workers on the node before cleaning up.
        """
        raise NotImplementedError()


def shutdownFileStore(workflowDir, workflowID):
    """
    Run the deferred functions from any prematurely terminated jobs still lingering on the system
    and carry out any necessary filestore-specific cleanup.

    This is a destructive operation and it is important to ensure that there are no other running
    processes on the system that are modifying or using the file store for this workflow.


    This is the intended to be the last call to the file store in a Toil run, called by the
    batch system cleanup function upon batch system shutdown.

    :param str workflowDir: The path to the cache directory
    :param str workflowID: The workflow ID for this invocation of the workflow
    """

    # Defer these imports until runtime, since these classes depend on our file
    from toil.fileStore.cachingFileStore import CachingFileStore
    from toil.fileStore.nonCachingFileStore import NonCachingFileStore

    cacheDir = os.path.join(workflowDir, cacheDirName(workflowID))
    if os.path.exists(cacheDir):
        # The presence of the cacheDir suggests this was a cached run. We don't need the cache lock
        # for any of this since this is the final cleanup of a job and there should be  no other
        # conflicting processes using the cache.
        CachingFileStore.shutdown(cacheDir)
    else:
        # This absence of cacheDir suggests otherwise.
        NonCachingFileStore.shutdown(workflowDir)


