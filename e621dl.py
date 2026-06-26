#!/usr/bin/env python3
"""e621dl — download e621 posts matching configured searches into a Drop Box folder.

Uses the v2 post API (v2=true; default mode=basic returns tags as a flat array).
Every downloaded post ID is recorded in database.txt, so a file deleted from the
Drop Box folder is never downloaded again.
"""

import configparser
import datetime
import logging
import os
import re
import sys
import time
from fnmatch import fnmatch

import requests

VERSION = '5.1.0'
USER_AGENT = f'e621dl-M/{VERSION} (+Manual on e621)'
POSTS_URL = 'https://e621.net/posts.json'
POOL_URL = 'https://e621.net/pools'
POOL_SUBDIR = 'pools'
PAGE_LIMIT = 320
REQUEST_DELAY = 0.5  # e621 API allows at most 2 requests per second
PARTIAL_EXT = '.part'

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.ini')
DATABASE_FILE = os.path.join(BASE_DIR, 'database.txt')

log = logging.getLogger('e621dl')

DEFAULT_CONFIG_TEXT = '''[Settings]
; Destination folder (e.g. the macOS Drop Box). Created if it does not exist.
download_directory = ~/Public/Drop Box
; Folder for pool albums. If omitted, defaults to a "pools" subfolder of
; download_directory.
; pool_directory = ~/Public/Drop Box/Pools

[Defaults]
days = 1
ratings = s, q, e
min_score = -100

[Blacklist]
tags =

; Pools are ordered albums. List their numeric IDs here (comma- or
; whitespace-separated). Each pool is saved, in order, to its own subfolder.
[Pools]
ids =

; Any other section is a search group:
; [some search]
; days = 30
; ratings = s
; min_score = 5
; tags = cat, cute
'''


class Search:
    def __init__(self, name, tags, ratings, min_score, earliest_date):
        self.name = name
        self.tags = tags
        self.ratings = ratings
        self.min_score = min_score
        self.earliest_date = earliest_date


def load_config():
    if not os.path.isfile(CONFIG_FILE):
        with open(CONFIG_FILE, 'wt', encoding='utf_8_sig') as outfile:
            outfile.write(DEFAULT_CONFIG_TEXT)
        log.error('No config file found. A default config.ini was created — add search groups to it.')
        raise SystemExit(1)

    config = configparser.ConfigParser()
    with open(CONFIG_FILE, 'rt', encoding='utf_8_sig') as infile:
        config.read_file(infile)
    return config


def parse_taglist(value):
    return value.replace(',', ' ').lower().strip().split()


def date_from_days(days):
    earliest = datetime.date.today() - datetime.timedelta(days=max(days - 1, 0))
    return max(earliest, datetime.date.fromordinal(1)).strftime('%Y-%m-%d')


def parse_searches(config):
    """Split config into (download_directory, pool_directory, blacklist, searches, pools).

    pool_directory is None when unset, to be resolved against download_directory.
    """
    download_directory = os.path.expanduser('~/Public/Drop Box')
    pool_directory = None
    blacklist = []
    default_days = 1
    default_score = -0x7FFFFFFF
    default_ratings = ['s', 'q', 'e']
    searches = []
    pools = []

    for section in config.sections():
        name = section.lower()
        if name == 'settings':
            value = config.get(section, 'download_directory', fallback=None)
            if value:
                download_directory = os.path.expanduser(value.strip())
            pool_value = config.get(section, 'pool_directory', fallback=None)
            if pool_value and pool_value.strip():
                pool_directory = os.path.expanduser(pool_value.strip())
        elif name == 'defaults':
            default_days = config.getint(section, 'days', fallback=default_days)
            default_score = config.getint(section, 'min_score', fallback=default_score)
            if config.get(section, 'ratings', fallback=''):
                default_ratings = parse_taglist(config.get(section, 'ratings'))
        elif name == 'blacklist':
            blacklist = parse_taglist(config.get(section, 'tags', fallback=''))
        elif name == 'pools':
            for token in config.get(section, 'ids', fallback='').replace(',', ' ').split():
                if token.isdigit():
                    pools.append(int(token))
                else:
                    log.warning('Ignoring invalid pool id: %s', token)
        elif name == 'other':
            pass  # legacy section, ignored
        else:
            tags = parse_taglist(config.get(section, 'tags', fallback=''))
            if not tags:
                log.warning('Search [%s] has no tags, skipping.', section)
                continue
            days = config.getint(section, 'days', fallback=default_days)
            min_score = config.getint(section, 'min_score', fallback=default_score)
            ratings_value = config.get(section, 'ratings', fallback='')
            ratings = parse_taglist(ratings_value) if ratings_value else default_ratings
            searches.append(Search(section, tags, ratings, min_score, date_from_days(days)))

    return download_directory, pool_directory, blacklist, searches, pools


def load_database():
    if not os.path.isfile(DATABASE_FILE):
        return set()
    with open(DATABASE_FILE, 'rt') as infile:
        return {line.strip() for line in infile if line.strip()}


def record_download(post_id):
    with open(DATABASE_FILE, 'at') as outfile:
        outfile.write(f'{post_id}\n')


def api_get(session, params):
    params = dict(params, v2='true')
    start = time.monotonic()
    response = session.get(POSTS_URL, params=params)
    elapsed = time.monotonic() - start
    if elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)
    response.raise_for_status()
    return response.json()


def get_page(session, search, last_id):
    query = f'id:<{last_id} score:>={search.min_score} date:>={search.earliest_date} ' + ' '.join(search.tags)
    return api_get(session, {'limit': PAGE_LIMIT, 'tags': query})


def file_url(post):
    url = post['files']['original'].get('url')
    if url:
        return url
    # The API sometimes returns a null file URL; reconstruct it from the md5.
    meta = post['files']['meta']
    md5, ext = meta['md5'], meta['ext']
    return f'https://static1.e621.net/data/{md5[:2]}/{md5[2:4]}/{md5}.{ext}'


def sanitize_name(name):
    """Make a pool name safe to use as a folder name."""
    cleaned = ''.join('_' if c in '\\/:*?"<>|' else c for c in name).strip()
    return cleaned or 'pool'


def download_file(session, post, path):
    """Download a post's file to an exact path, via a .part temp file."""
    partial = path + PARTIAL_EXT
    response = session.get(file_url(post), stream=True)
    response.raise_for_status()
    with open(partial, 'wb') as outfile:
        for chunk in response.iter_content(chunk_size=65536):
            outfile.write(chunk)
    os.rename(partial, path)


def download_post(session, post, directory):
    ext = post['files']['meta']['ext']
    timestamp = time.strftime('%Y%m%d_%H%M%S_', time.localtime())
    download_file(session, post, os.path.join(directory, f"{timestamp}{post['id']}.{ext}"))


def clean_partial_downloads(directory):
    for entry in os.listdir(directory):
        if entry.endswith(PARTIAL_EXT):
            log.info('Removing incomplete download: %s', entry)
            os.remove(os.path.join(directory, entry))


def run_search(session, search, blacklist, database, directory):
    downloaded = in_storage = bad_rating = blacklisted = 0
    last_id = 0x7FFFFFFF

    while True:
        posts = get_page(session, search, last_id)

        for post in posts:
            if str(post['id']) in database:
                in_storage += 1
            elif post['rating'] not in search.ratings:
                bad_rating += 1
            elif any(fnmatch(tag, pattern) for tag in post['tags'] for pattern in blacklist):
                blacklisted += 1
            else:
                download_post(session, post, directory)
                database.add(str(post['id']))
                record_download(post['id'])
                downloaded += 1

        if len(posts) < PAGE_LIMIT:
            break
        last_id = posts[-1]['id']

    log.info('[%s] downloaded %d, already saved %d, wrong rating %d, blacklisted %d',
             search.name, downloaded, in_storage, bad_rating, blacklisted)


def get_pool_entry(session, pool_id):
    """Fetch a pool's metadata (legacy shape: name + ordered post_ids)."""
    start = time.monotonic()
    response = session.get(f'{POOL_URL}/{pool_id}.json')
    elapsed = time.monotonic() - start
    if elapsed < REQUEST_DELAY:
        time.sleep(REQUEST_DELAY - elapsed)
    response.raise_for_status()
    return response.json()


def get_pool_posts(session, pool_id):
    """Fetch every post in a pool (v2), keyed by id. Order is restored later."""
    posts = {}
    last_id = 0x7FFFFFFF
    while True:
        page = api_get(session, {'limit': PAGE_LIMIT,
                                 'tags': f'pool:{pool_id} order:id_desc id:<{last_id}'})
        for post in page:
            posts[post['id']] = post
        if len(page) < PAGE_LIMIT:
            return posts
        last_id = page[-1]['id']


def existing_pool_post_ids(directory):
    """Post IDs already present on disk in a pool folder (parsed from filenames)."""
    ids = set()
    for entry in os.listdir(directory):
        match = re.match(r'\d+_(\d+)\.', entry)
        if match:
            ids.add(int(match.group(1)))
    return ids


def run_pool(session, pool_id, blacklist, pool_directory):
    try:
        entry = get_pool_entry(session, pool_id)
    except requests.HTTPError as error:
        log.error('Pool %s could not be fetched: %s', pool_id, error)
        return
    if not isinstance(entry, dict) or 'id' not in entry:
        log.error('Pool %s not found.', pool_id)
        return

    name = entry.get('name') or f'pool_{pool_id}'
    order = entry.get('post_ids', [])
    directory = os.path.join(pool_directory, sanitize_name(name))
    os.makedirs(directory, exist_ok=True)
    clean_partial_downloads(directory)

    # Dedup against the live files on disk: a post deleted from the album folder
    # is downloaded again, keeping the album complete.
    on_disk = existing_pool_post_ids(directory)
    posts = get_pool_posts(session, pool_id)

    downloaded = in_storage = blacklisted = unavailable = 0
    # Iterate in the pool's canonical order so page numbers match the album.
    for page_number, post_id in enumerate(order, start=1):
        post = posts.get(post_id)
        if post is None:
            unavailable += 1  # deleted or otherwise not returned by the search
            continue

        if post_id in on_disk:
            in_storage += 1
        elif any(fnmatch(tag, pattern) for tag in post['tags'] for pattern in blacklist):
            blacklisted += 1
        else:
            ext = post['files']['meta']['ext']
            path = os.path.join(directory, f'{page_number:04d}_{post_id}.{ext}')
            download_file(session, post, path)
            downloaded += 1

    log.info('[pool %s "%s"] downloaded %d, already on disk %d, blacklisted %d, unavailable %d',
             pool_id, name, downloaded, in_storage, blacklisted, unavailable)


def main():
    logging.basicConfig(level=logging.INFO, format='%(name)-7s %(levelname)-8s %(message)s')
    log.info('Running e621dl %s', VERSION)

    config = load_config()
    directory, pool_directory, blacklist, searches, pools = parse_searches(config)
    if not searches and not pools:
        log.error('No search groups or pools defined in config.ini.')
        raise SystemExit(1)
    if pool_directory is None:
        pool_directory = os.path.join(directory, POOL_SUBDIR)

    os.makedirs(directory, exist_ok=True)
    clean_partial_downloads(directory)
    database = load_database()
    log.info('Saving to %s (%d entries already in database)', directory, len(database))

    with requests.Session() as session:
        session.headers['User-Agent'] = USER_AGENT
        for search in searches:
            run_search(session, search, blacklist, database, directory)
        if pools:
            log.info('Saving pools to %s', pool_directory)
            os.makedirs(pool_directory, exist_ok=True)
            for pool_id in pools:
                run_pool(session, pool_id, blacklist, pool_directory)

    log.info('All downloads complete.')


if __name__ == '__main__':
    main()
