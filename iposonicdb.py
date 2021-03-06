#
# Subsonic API uses those three items
#  for storing songs, albums and artists
#  Those entities require and id
# -*- coding: utf-8 -*-
#from __future__ import unicode_literals

import os
import sys
import time
from os.path import join, basename

# logging
import logging
from sqlalchemy import orm
logging.basicConfig(level=logging.INFO)

from iposonic import (
    IposonicException, EntryNotFoundException,
    ArtistDAO, AlbumDAO, MediaDAO, PlaylistDAO,
    UserDAO, UserMediaDAO
)
from mediamanager import MediaManager, UnsupportedMediaError
from mediamanager.stringutils import to_unicode

# add local path for loading _mysqlembedded
sys.path.insert(0, './lib')
try:
    import _mysqlembedded
    sys.modules['_mysql'] = _mysqlembedded
except:
    #Fall back to mysql server module
    pass

# SqlAlchemy for ORM
from sqlalchemy import Table, Column, Integer, String, MetaData, ForeignKey
from sqlalchemy import create_engine, desc
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm import scoped_session
from sqlalchemy.orm.query import Query
from sqlalchemy.ext.declarative import declarative_base, DeclarativeMeta
from sqlalchemy.exc import ProgrammingError, OperationalError

from threading import Lock


def synchronized(lock):
    """ Synchronization decorator. """

    def wrap(f):
        def newFunction(*args, **kw):
            lock.acquire()
            try:
                return f(*args, **kw)
            finally:
                lock.release()
        return newFunction
    return wrap


class LazyDeveloperMeta(DeclarativeMeta):
    """This class allows a lazy initialization of DAOs.

       Just add __tablename__ and __fields__ attribute to a subclass
       to associate a table.

       Should subclass DeclarativeMeta because it should contain Base initialization methods.

       TODO: make customizable columns types, but it's ok for small collections ;)
       """
    def __init__(klass, classname, bases, dict_):
        """ Create a new class type.

            DeclarativeMeta stores class attributes in dict_
         """
        # Additionally, set attributes on the new object.
        is_pk = True
        for name in dict_.get('__fields__', []):
            if name in ['id', 'duration']:
                kol = Integer()
            elif name in ['path', 'entry']:
                kol = String(192)
            else:
                kol = String(64)
            setattr(
                klass, name, Column(name, kol, primary_key=is_pk))
            is_pk = False

        # Return the new object using super().
        return DeclarativeMeta.__init__(klass, classname, bases, dict_)

Base = declarative_base(metaclass=LazyDeveloperMeta)


class IposonicDBTables:
    """DAO classes and Serializing methods.

        Table definition and data gathering is moved
        to iposonic.*DAO, so that it's shared with
        MemoryIposonicDB
    """
    class SerializerMixin(object):
        """Methods for serializing DAO and expose a dict-like behavior.

            __fields__ and __tablename__  see  iposonic.py
        """
        __fields__ = []
        log = logging.getLogger(__name__)

        def json(self):
            """Return a dict/json representation of the public fields of
                the object.

            """
            ret = []
            for (k, v) in self.__dict__.iteritems():
                # None entries will be serialized to null
                #   so skip them
                if v is None:
                    continue
                # TODO we could just cycle for
                # k in self.__fields__ or intersect
                # directly in the first part
                if k in self.__fields__:
                    if k.lower() == 'isdir':
                        v = (v.lower() == 'true')
                    elif k == 'path':
                        # for future use
#                        for field in ['artist', 'name', 'Author']:
#                            artist = self.get(field,'')
#                            if artist:
#                                break
#                        artist = artist if artist else 'Unknown Artist'
#                        album = self.get('album') if self.get('album') else 'Unknown Album'
#
#                        title = self.get('title','') if self.get('title') else 'Unknown song'
#                        assert artist != None, "Can't find artist: %s" % dict.__repr__(self.__dict__)
#                        assert album != None
#                        #assert title
#                        v = os.path.join(artist, album, title)
                        pass
                    elif k.lower() in ['userrating',
                                       'averagerating',
                                       'duration',
                                       'bitrate']:
                        v = int(v) if v is not None else 0
                    ret.append((k, v))
            return dict(ret)

        def get(self, attr, default=None):
            """Expose __dict__.get"""
            return self.__dict__.get(attr, default)

        def update(self, dict_):
            """Expose __dict__.update"""
            return self.__dict__.update(dict_)

        def __repr__(self):
            return "<%s: %s>" % (
                self.__class__.__name__,
                self.json().__repr__())

    class Artist(ArtistDAO, Base, SerializerMixin):
        __fields__ = ArtistDAO.__fields__

        def __init__(self, path_u):
            Base.__init__(self)
            self.update(self.get_info(path_u))

    class Media(Base, SerializerMixin, MediaDAO):
        __fields__ = MediaDAO.__fields__

        def __init__(self, path):
            """Fill entry using MediaManager.get_info.

            """
            Base.__init__(self)
            self.update(MediaManager.get_info(path))

    class Album(Base, SerializerMixin, AlbumDAO):
        __fields__ = AlbumDAO.__fields__

        def __init__(self, path, name=None):
            Base.__init__(self)
            self.update(self.get_info(path))

    class Playlist(Base, SerializerMixin, PlaylistDAO):
        __fields__ = PlaylistDAO.__fields__

        def __init__(self, name):
            Base.__init__(self)
            self.update(self.get_info(name))

    class User(Base, SerializerMixin, UserDAO):
        __fields__ = UserDAO.__fields__

        def __init__(self, username):
            Base.__init__(self)
            self.update({
                'id': MediaManager.uuid(username),
                'username': username}
            )

    class UserMedia(Base, SerializerMixin, UserMediaDAO):
        __fields__ = UserMediaDAO.__fields__

        def __init__(self, email, mid):
            Base.__init__(self)
            self.update({'email': email, 'mid': mid})


class SqliteIposonicDB(object, IposonicDBTables):
    """Store data on Sqlite

        To use this class you have to:
        # instantiate
        db = SqliteIposonicDB()
        # initialize db (required for embedded)
        db.init_db()

        This object implements connection to specific databases using two
        decorators:
        - connectable for read
        - transactional for write

        The DAO part is inherited from IposonicDBTables, containing:
        - Album
        - Artist
        - Media
        - Playlist
        - User


    """
    log = logging.getLogger('SqliteIposonicDB')
    engine_s = "sqlite"
    sql_lock = Lock()

    @synchronized(sql_lock)
    def connectable(fn):
        """add connectable semantics to a method.

        """
        def connect(self, *args, **kwds):
            session = self.Session()
            kwds['session'] = session
            try:
                ret = fn(self, *args, **kwds)
                return ret
            except (ProgrammingError, OperationalError) as e:
                self.log.exception(
                    "Corrupted database: removing and recreating", e)
                self.reset()
            except orm.exc.NoResultFound as e:
                # detailed logging for NoResultFound isn't needed.
                # just propagate the exception
                raise EntryNotFoundException(e)
            except Exception as e:
                if len(args):
                    ret = to_unicode(args[0])
                else:
                    ret = ""
                self.log.exception(
                    u"error: string: %s, ex: %s" % (ret.__class__, e))
                raise
        connect.__name__ = fn.__name__
        return connect

    @synchronized(sql_lock)
    def transactional(fn):
        """add transactional semantics to a method.

        """
        def transact(self, *args, **kwds):
            session = self.Session()
            kwds['session'] = session
            try:
                ret = fn(self, *args, **kwds)
                session.commit()
                return ret
            except (ProgrammingError, OperationalError) as e:
                session.rollback()
                self.log.exception(
                    "Corrupted database: removing and recreating")
                self.reset()
            except Exception as e:
                session.rollback()
                if len(args):
                    ret = to_unicode(args[0])
                else:
                    ret = ""
                self.log.exception(
                    u"error: string: %s, ex: %s" % (ret.__class__, e))
                raise
        transact.__name__ = fn.__name__
        return transact

    def __init__(self, music_folders, dbfile="iposonic1",
                 refresh_interval=60, user="iposonic", passwd="iposonic",
                 host="localhost", recreate_db=False, datadir="/tmp/iposonic"):
        self.music_folders = music_folders

        # database credentials
        self.dbfile = dbfile
        self.user = user
        self.passwd = passwd
        self.host = host

        # sql alchemy db connector
        self.engine = create_engine(
            self.create_uri(), echo=False, convert_unicode=True, encoding='utf8')

        #self.engine.raw_connection().connection.text_factory = str
        self.Session = scoped_session(sessionmaker(bind=self.engine))
        self.initialized = 0
        self.refresh_interval = refresh_interval
        self.indexes = dict()
        self.log.setLevel(logging.INFO)
        self.initialized = False
        self.recreate_db = recreate_db
        self.datadir = datadir
        assert self.log.isEnabledFor(logging.INFO)

    def create_uri(self):
        if self.engine_s == 'sqlite':
            return "%s:///%s" % (self.engine_s, self.dbfile)
        elif self.engine_s.startswith('mysql'):
            return "%s://%s:%s@%s/%s?charset=utf8" % (
                self.engine_s,
                self.user,
                self.passwd,
                self.host,
                self.dbfile)

    def init_db(self):
        """On sqlite does nothing."""
        if self.recreate_db:
            self.reset()

    def end_db(self):
        pass

    def reset(self):
        """Drop and recreate database. Reinstantiate session."""
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)

    def _query_and_format(self, table_o, query, eid=None, order=None, session=None):
        """Query and return json entries  .

           this method can't be use for modifying items

        """
        ret = self._query(
            table_o, query, eid=eid, order=order, session=session)
        if eid:
            return ret.json()
        if ret:
            return [r.json() for r in ret]
        return []

    #
    # Query a-la-sqlalchemy supporting ordering and filtering
    #
    def _query(self, table_o, query, eid=None, order=None, session=None):
        """Query db and return database objects.

        """
        assert table_o, "Table must not be null"
        order_f = None
        qmodel = session.query(table_o)
        if eid:
            rs = qmodel.filter_by(id=eid).one()
            return rs

        # Multiple results support ordering
        if order:
            (order_f, is_desc) = order
            order_f = table_o.__getattribute__(table_o, order_f)
            self.log.debug("order: %s" % [order])
            if is_desc:
                order_f = order_f.desc()

        # TODO does it works with (k,v) = query.popitem()?
        if query:
            for (k, v) in query.items():
                field_o = table_o.__getattribute__(table_o, k)
                assert field_o, "Field must not be null"
                if v == 'isNull':
                    rs = qmodel.filter(field_o == None)
                elif v == 'notNull':
                    rs = qmodel.filter(field_o != None)
                else:
                    rs = qmodel.filter(field_o.like("%%%s%%" % v))
        else:
            rs = qmodel
        if not rs:
            return []
        return  rs.order_by(order_f).all()

    def _query_id(self, eid, table=None, session=None):
        """Get an entry by id. If table is unspecified
           it will search in any.
        """
        assert eid, "Missing eid"
        table_l = [self.Media, self.Album, self.Artist, self.Playlist]
        if table:
            table_l = [table]
        for table_o in table_l:
            qmodel = session.query(table_o)
            try:
                rs = qmodel.filter_by(id=eid)
                if rs.one():
                    return rs
            except:
                pass
        raise ValueError("Eid not in db: %s" % eid)

    def _query_top(self, table_o, field_o, limit=20, session=None):
        """Return a list of songs, in json"""
        assert table_o and field_o
        qmodel = session.query(table_o)
        rs = qmodel.order_by(field_o.desc()).limit(limit)
        if not rs:
            return []
        return [r.json() for r in rs.all()]

    #
    # User management
    #
    @connectable
    def get_users(self, eid=None, query=None, session=None):
        assert session
        self.log.info("get_users: eid: %s, query: %s" % (eid, query))
        return self._query_and_format(self.User, query, eid=eid, session=session)

    #@transactional
    def add_user(self, user):
        entry = self.User(user.get('username'))
        entry.update(user)
        self.log.info("add_users: eid: %s, new: %s" % (entry, user))
        return self.create_entry(entry)

    @transactional
    def update_user(self, eid, new, session=None):
        assert session
        self.log.info("get_users: eid: %s, new: %s" % (eid, new))
        old = self._query_id(
            eid, table=self.User, session=session).update(new)
        self.log.info("user found, updating: %s" % old)

    @transactional
    def delete_user(self, eid, session=None):
        assert session, "Missing Session"
        assert eid, "Missing eid"
        old = self._query_id(eid, table=self.User, session=session).delete()
        self.log.info("user correctly deleted")
    #
    # Media management
    #

    @connectable
    def get_song_list(self, eids=[], session=None):
        """return iterable"""
        ret = []
        for k in eids:
            if k is None:
                continue
            try:
                ret.append(self.get_songs(eid=k))
            except Exception as e:
                self.log.warn("error retrieving %s due %s" % (k, e))
        return ret

    @transactional
    def get_highest(self, session=None):
        return self._query_top(self.Media, self.Media.userRating, session=session)

    @connectable
    def get_songs(self, eid=None, query=None, session=None):
        assert session
        self.log.info("get_songs: eid: %s, query: %s" % (eid, query))
        return self._query_and_format(self.Media, query, eid=eid, session=session)

    @connectable
    def get_albums(self, eid=None, query=None, order=None, session=None):
        self.log.info("get_albums: eid: %s, query: %s" % (eid, query))
        return self._query_and_format(self.Album, query, eid=eid, order=order, session=session)

    @connectable
    def get_playlists(self, eid=None, query=None, session=None):
        self.log.info("get_playlists: eid: %s, query: %s" % (eid, query))
        return self._query_and_format(self.Playlist, query, eid=eid, session=session)

    @connectable
    def get_artists(self, eid=None, query=None, order=None, session=None):
        """This method should trigger a filesystem initialization.

            returns a dict-array [{'id': .., 'name': .., 'path': .. }]

        """
        self.log.info("get_artists: %s" % eid)
        return self._query(self.Artist, query, eid=eid, order=order, session=session)

    def get_indexes(self):
        """Create a subsonic index getting artists from the database."""
        #
        # indexes = { 'A' : {'artist': {'id': .., 'name': ...}}}
        #
        indexes = dict()
        for artist_j in self.get_artists(order=('name', 1)):
            a = artist_j.get('name')
            artist_j = artist_j.json()
            if not a:
                continue
            first = a[0:1].upper()
            try:
                indexes[first].append({'artist': artist_j})
            except KeyError:
                indexes[first] = [{'artist': artist_j}]
        return indexes

    def get_music_folders(self):
        return self.music_folders

    @transactional
    def create_entry(self, entry, session=None):
        assert entry, "Entry is null"
        session.merge(entry)
        return entry.get('id')

    @transactional
    def update_entry(self, eid, new, session=None):
        assert session, "Missing Session"
        assert eid, "Missing eid"
        assert new, "Missing new object"
        old = self._query_id(eid, session=session).update(new)

    @transactional
    def delete_entry(self, eid, session=None):
        assert session, "Missing Session"
        assert eid, "Missing eid"
        old = self._query_id(eid, session=session).delete()

    @transactional
    def add_path(self, path, album=False, session=None):
        self.log.info("add_path: %s, album=%s" % (path, album))
        assert session
        eid = None
        record = None
        record_a = None
        if not isinstance(path, unicode):
            path_u = to_unicode(path)
        else:
            path_u = path

        if os.path.isdir(path):
            eid = MediaManager.uuid(path)
            if album:
                record = self.Album(path)
            else:
                record = self.Artist(path)
            self.log.info("adding directory: %s, %s " % (eid, path_u))
        elif MediaManager.is_allowed_extension(path_u):
            try:
                record = self.Media(path)
                # Create a virtual album using a mock album id
                #   every song with the same virtual album (artist,album)
                #   is tied to it.
                if record.album != basename(path) and record.artist and record.album:
                    vpath = join("/", record.artist, record.album)
                    record_a = self.Album(vpath)
                    record.albumId = MediaManager.uuid(vpath)
                eid = record.id
                self.log.info("adding file: %s, %s " % (
                    eid, path_u))
            except UnsupportedMediaError, e:
                raise IposonicException(e)

        if record and eid:
            record.update({'created': int(os.stat(path).st_ctime)})

            self.log.info("Adding entry: %s " % record)
            session.merge(record)
            if record_a:
                session.merge(record_a)
            return eid

        raise IposonicException("Path not found or bad extension: %s " % path)

    @transactional
    def walk_music_directory_depecated(self, session=None):
        """Find all artists (top-level directories) and create indexes.

          TODO: use ctime|mtime or inotify to avoid unuseful I/O.
        """
        self.log.info("walking: %s" % self.get_music_folders())

        if time.time() - self.initialized < self.refresh_interval:
            return

        # reset database
        #self.reset()
        def add_or_log(self, path):
            try:
                self.add_path(path, session=session)
            except IposonicException as e:
                self.log.error(e)
        # find all artists
        for music_folder in self.get_music_folders():
            artists_local = [x for x in os.listdir(
                music_folder) if os.path.isdir(join("/", music_folder, x))]

            #index all artists
            for a in artists_local:
                try:
                    self.log.info(u"scanning artist: %s" % a)
                except:
                    self.log.info(u'cannot read object: %s' % a.__class__)
                if a:
                    path = join("/", music_folder, a)
                    add_or_log(self, path)
                if self.refresh_interval:
                    continue
                #
                # Scan recurrently only if not refresh_always
                #
                for dirpath, dirnames, filenames in os.walk(path):
                    for f in filenames:
                        add_or_log(self, join("/", path, dirpath, f))
        #
        # We're ok now
        #
        self.initialized = time.time()


class MySQLIposonicDB(SqliteIposonicDB):
    """MySQL standard and embedded version.

        Classic version requires uri, otherwise
        you need to play with embedded.
    """
    # mysql embedded
    import _mysqlembedded as _mysql

    log = logging.getLogger('MySQLIposonicDB')
    engine_s = "mysql+mysqldb"
    driver = _mysql

    @synchronized(SqliteIposonicDB.sql_lock)
    def end_db(self):
        """MySQL requires teardown of connections and memory structures."""
        if self.initialized and self.driver:
            self.driver.server_end()

    @synchronized(SqliteIposonicDB.sql_lock)
    def init_db(self):
        if self.initialized:
            return
        self.log.info("initializing database in %s" % self.datadir)
        if not os.path.isdir(self.datadir):
            os.mkdir(self.datadir)
        self.driver.server_init(
            ['ipython', "--no-defaults", "-h", self.datadir, '--bootstrap'], ['ipython_CLIENT', 'ipython_SERVER', 'embedded'])

        conn = self.driver.connection(user=self.user, passwd=self.passwd)
        try:
            conn.autocommit(True)

            conn.query("create database if not exists %s ;" % self.dbfile)
            conn.store_result()

            conn.query("use %s;" % self.dbfile)
            conn.store_result()

            conn.query("create table if not exists iposonic(version text);")
            conn.store_result()
            conn.query("insert into iposonic(version) values('0.0.1');")
            conn.store_result()
            assert not conn.error()
        except:
            raise
        finally:
            conn.close()
        if self.recreate_db:
            self.reset()
        self.initialized = True
        #_mysql.server_end()
