import os
import sqlite3
import lz4
import io
from time import time
from email.utils import mktime_tz, parsedate_tz
from collections import namedtuple
from tornado import httpclient

try:
    lz4_decompress = lz4.loads
    print("Warning: You're using an outdated LZ4 library. Please update it with pip.")
except AttributeError:
    import lz4.block
    lz4_decompress = lz4.block.decompress

DBMANIFEST = "https://asset-starlight-stage.akamaized.net/dl/{0}/manifests"
ASSETBBASEURL = "https://asset-starlight-stage.akamaized.net/dl/resources/AssetBundles"
SOUNDBASEURL = "https://asset-starlight-stage.akamaized.net/dl/resources/Sound"
SQLBASEURL = "https://asset-starlight-stage.akamaized.net/dl/resources/Generic/{1}/{0}"
CACHE = os.path.join(os.path.dirname(__file__), "__manifestloader_cache")
try:
    os.makedirs(CACHE, 0o755)
except FileExistsError:
    pass

def extra_acquisition_headers():
    return {"X-Unity-Version": os.environ.get("VC_UNITY_VER", "5.4.5p1")}

def filename(version, platform, asset_qual, sound_qual):
    return "{0}_{1}_{2}_{3}".format(version, platform, asset_qual, sound_qual)

def read_manifest(version, platform, asset_qual, sound_qual, callback):
    dest_file = os.path.join(CACHE, filename(version, platform, asset_qual, sound_qual))

    def acquire_complete(path):
        if not path:
            return callback(None)

        conn = sqlite3.connect(path)
        callback(conn)

    if not os.path.exists(dest_file):
        acquire_manifest(version, platform, asset_qual, sound_qual, dest_file, acquire_complete)
    else:
        acquire_complete(dest_file)

manifest_selector_t = namedtuple("manifest_selector_t", ("filename", "md5", "platform", "asset_qual", "sound_qual"))
def acquire_manifest(version, platform, asset_qual, sound_qual, dest_file, callback):
    cl = httpclient.AsyncHTTPClient()

    def read_manifest(response):
        print("trace read_manifest", response)

        if response.error:
            return callback(None)

        buf = response.buffer.read()
        bio = io.BytesIO()
        bio.write(buf[4:8])
        bio.write(buf[16:])
        data = lz4_decompress(bio.getvalue())
        with open(dest_file, "wb") as write_db:
            write_db.write(data)

        callback(dest_file)

    def read_meta_manifest(response):
        print("trace read_meta_manifest", response)
        if response.error:
            return callback(None)

        m = response.body.decode("utf8")
        mp = map(lambda x: manifest_selector_t(* x.split(",")), filter(bool, m.split("\n")))
        get_file = None
        for selector in mp:
            if selector.platform == platform and \
               selector.asset_qual == asset_qual and \
               selector.sound_qual == sound_qual:
                get_file = selector.filename
                break
        else:
            print("No such candidate found for", platform, asset_qual, sound_qual)
            return callback(None)

        abso = "/".join(( DBMANIFEST.format(version), get_file ))
        cl.fetch(abso, read_manifest, headers=extra_acquisition_headers())

    meta = "/".join(( DBMANIFEST.format(version), "all_dbmanifest" ))
    cl.fetch(meta, read_meta_manifest, headers=extra_acquisition_headers())

def get_master(res_ver, to_path, done):
    print("trace get_master", res_ver, to_path, done)

    def got_master(response):
        print("trace got_master", response)

        if response.error:
            return done(None)

        buf = response.buffer.read()
        bio = io.BytesIO()
        bio.write(buf[4:8])
        bio.write(buf[16:])
        data = lz4_decompress(bio.getvalue())
        with open(to_path, "wb") as write_db:
            write_db.write(data)

        mdate = response.headers.get("Last-Modified")
        if mdate:
            tt = parsedate_tz(mdate)
            mtime = mktime_tz(tt) if tt else int(time())
        else:
            mtime = int(time.time())
        os.utime(to_path, (-1, mtime))
        done(to_path)

    def got_manifest(connection):
        print("trace got_manifest", connection)

        if not connection:
            return done(None)

        cur = connection.execute("SELECT hash, attr FROM manifests WHERE name = ?", ("master.mdb",))
        hash, attr = cur.fetchone()
        connection.close()

        url = SQLBASEURL.format(hash, hash[0:2])
        cl = httpclient.AsyncHTTPClient()
        cl.fetch(url, got_master, headers=extra_acquisition_headers())

    read_manifest(res_ver, "Android", "High", "High", got_manifest)
