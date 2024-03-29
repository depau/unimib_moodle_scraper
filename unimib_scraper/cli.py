"""
Usage: ARGV0 [-j COOKIEJAR] [-d DESTDIR] [-t TRANSFERS] [-k VIDEOS_JSON] [-u username] [-p password]

If username and password are not specified, they will be fetched from the environment variables
UNIMIB_USERNAME and UNIMIB_PASSWORD.

Options:
    --help, -h                                 Show this screen.
    --version, -v                              Show version.
    --cookiejar=COOKIEJAR, -j COOKIEJAR        Path to the cookies persistence file [default: cookies.json]
    --destdir=DESTDIR, -d DESTDIR              Destination directory [default: .]
    --transfers=TRANSFERS, -t TRANSFERS        Number of parallel transfers [default: 12]
    --videos-json=VIDEOS_JSON, -k VIDEOS_JSON  Path to a JSON where the lesson video URLs will be stored [default: videos.json]
    --password=PASSWORD, -p PASSWORD           Your password
    --username=USER, -u USER                   Username
"""
import json
import multiprocessing
import multiprocessing.pool
import os
import re
import sys
import time
from collections import namedtuple
from pathlib import Path
from typing import List, Union, Mapping
from urllib.parse import parse_qs, urlparse

import enlighten
from bs4 import BeautifulSoup
from docopt import docopt
from moodle import Moodle

from unimib_scraper import Urls
from unimib_scraper.browser_session import BrowserSession

argv0 = sys.argv[0]
if "__main__.py" in argv0:
    argv0 = "unimib_scraper"

doc = __doc__.replace("ARGV0", argv0)

Course = namedtuple("Course", ["id", "category", "name"])

LANG_REGEX = re.compile(r"{mlang (?P<lang>\w+)}(?P<name>.*?){mlang}")

IGNORED_MODULES = [
    "assign",
    "choice",
    "choicegroup",
    "feedback",
    "forum",
    "label",
    "quiz",
    "page",
    "customcert",
    "scorm",
]

WIN_FORBIDDEN_FILENAMES = [
    "CON",
    "PRN",
    "AUX",
    "NUL",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "COM0",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
    "LPT0",
]

WIN_FORBIDDEN_FILENAME_CHARMAP = {
    "\\": "∖",
    ":": "∶",
    "*": "∗",
    "?": "？",
    '"': "＂",
    "<": "＜",
    ">": "＞",
    "|": "∣",
}


def pad_desc(desc: Union[str, list]):
    if isinstance(desc, list):
        if len(desc) == 0:
            return pad_desc("???")
        desc = desc[-1]

    max_len = 35
    if len(desc) > max_len:
        return desc[: max_len - 3] + "..."
    return desc.ljust(max_len)


def escape_path_name(name: str):
    name = name.replace("/", "⁄")
    if sys.platform == "win32":
        if name in WIN_FORBIDDEN_FILENAMES:
            name = f"_{name}"
        for char, replacement in WIN_FORBIDDEN_FILENAME_CHARMAP.items():
            name = name.replace(char, replacement)
    return name


def escape_path(path: List[str]):
    return [escape_path_name(p) for p in path]


def get_lang_or_first(string: str, preferred_lang: str = "it") -> str:
    matches = LANG_REGEX.findall(string)
    if len(matches) == 0:
        return string
    for lang, name in matches:
        if lang == preferred_lang:
            return name
    return matches[0][1]


class WorkerPool:
    def __init__(self, nproc: int):
        self.pool = multiprocessing.pool.ThreadPool(nproc)
        self.semaphore = multiprocessing.Semaphore(nproc)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.pool.terminate()
        else:
            self.pool.close()
        self.pool.join()

    def submit(self, func, *args, **kwargs):
        self.semaphore.acquire()
        return self.pool.apply_async(func, args, kwargs, self._on_complete)

    def _on_complete(self, _):
        self.semaphore.release()


# noinspection PyMethodMayBeStatic
class Scraper:
    def __init__(
        self,
        browser: BrowserSession,
        moodle: Moodle,
        destdir: str,
        videos_json: str,
        transfers: int = 12,
    ):
        self.browser = browser
        self.moodle = moodle
        self.destdir = destdir
        self.videos_json = videos_json

        if os.path.exists(videos_json):
            with open(videos_json, "r") as f:
                self.videos = json.load(f)
        else:
            self.videos = {}

        self.site_info = moodle.core.webservice.get_site_info()

        self.progress = enlighten.get_manager(threaded=True)
        self.status_bar = self.progress.status_bar(
            program=f"Scraping {self.site_info.sitename}",
            status="0 B/s",
            status_format="{program}{fill}{status}",
            color="black_on_white",
            position=1,
        )

        self.pool = WorkerPool(transfers)

        self._last_progress_update = 0
        self._downloaded_bytes = 0

        self.categories: Mapping[int, dict] = {}

    def __enter__(self):
        self.pool.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.pool.__exit__(exc_type, exc_val, exc_tb)

    def _notify_downloaded_bytes(self, bytes_downloaded: int):
        if self._last_progress_update == 0:
            self._last_progress_update = time.time()
        self._downloaded_bytes += bytes_downloaded

        if time.time() - self._last_progress_update > 1:
            self.status_bar.update(
                status=f"{self._downloaded_bytes / 1024 / 1024:.2f} MiB/s"
            )
            self._downloaded_bytes = 0
            self._last_progress_update = time.time()

    def scrape(self):
        print("Fetching course categories...")
        categories = self.moodle(
            "core_course_get_categories", criteria=[], moodlewssettinglang="it"
        )

        out_categories = {}
        for category in categories:
            category["name"] = get_lang_or_first(category["name"])
            category["path"] = [int(i) for i in category["path"].split("/") if i]
            out_categories[category["id"]] = category

        self.categories = out_categories

        print("Fetching courses...")
        courses = self.moodle(
            "core_enrol_get_users_courses",
            userid=self.site_info.userid,
            returnusercount=0,
            moodlewssettingfilter=True,
            moodlewssettingfileurl=True,
            moodlewssettinglang="it",
        )

        with self.progress.counter(
            total=len(courses),
            desc=pad_desc("Downloading courses"),
            unit="courses",
            leave=False,
        ) as progress:
            for course in courses:
                if course["category"] and course["category"] in self.categories:
                    category = self.categories[course["category"]]
                    path = [self.categories[i]["name"] for i in category["path"]]
                else:
                    path = []
                path.append(get_lang_or_first(course["fullname"]))

                print(f"Checking course {' / '.join(path)}")

                # The moodlepy implementation of core_course_get_contents is broken
                content = self.moodle(
                    "core_course_get_contents",
                    courseid=course["id"],
                    options=[
                        {"name": "excludemodules", "value": "0"},
                        {"name": "excludecontents", "value": "0"},
                        {"name": "includestealthmodules", "value": "1"},
                    ],
                )

                self.scrape_course(path, content)
                progress.update()

    def fix_download_plugin_url(self, url):
        if "/webservice/pluginfile.php" not in url:
            return url
        return (
            url.replace(
                "/webservice/pluginfile.php",
                f"/tokenpluginfile.php/{self.site_info.userprivateaccesskey}",
            )
            + "&offline=1"
        )

    def _do_download(self, file: Path, url: str):
        try:
            with self.browser.get(url, stream=True) as r:
                with self.progress.counter(
                    total=int(r.headers.get("Content-Length", 0)),
                    desc=pad_desc(file.name),
                    unit="B",
                    leave=False,
                ) as progress:
                    length = int(r.headers.get("Content-Length", 0))
                    # Check if length matches existing file
                    if file.exists() and file.stat().st_size == length:
                        print(f"   - Skipping already downloaded: '{file}'")
                        return

                    with open(file, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                            progress.update(len(chunk))
                            self._notify_downloaded_bytes(len(chunk))
        except (Exception, KeyboardInterrupt, SystemExit):
            file.unlink(missing_ok=True)
            raise

    def download_resources(self, path: List[str], module: dict):
        progress = None

        def bump_progress():
            if progress is not None:
                progress.update()

        if "contents" not in module:
            print(f" - {' / '.join(path)}: Skipping empty module")
            return

        if len(module["contents"]) > 1:
            path = path + [module["name"]]
            progress = self.progress.counter(
                total=len(module["contents"]),
                desc=pad_desc(path),
                unit="files",
                leave=False,
            )

        try:
            for content in module["contents"]:
                if content["type"] != "file":
                    print(
                        f" - {' / '.join(path)}: Skipping non-file resource '{content['filename']}' (type: {content['type']})"
                    )
                    continue
                print(
                    f" - {' / '.join(path)}: Downloading file '{content['filename']}'"
                )
                fileurl = self.fix_download_plugin_url(content["fileurl"])

                escaped_path = escape_path(path + [content["filename"]])
                file = Path(os.path.join(self.destdir, *escaped_path))

                filesize = content["filesize"]
                if file.exists() and file.stat().st_size == filesize:
                    print(f"   - Skipping already downloaded: '{file}'")
                    bump_progress()
                    continue

                file.parent.mkdir(parents=True, exist_ok=True)
                self.pool.submit(self._do_download, file, fileurl)
                bump_progress()
        finally:
            if progress is not None:
                progress.close()

    def save_kaltura_video_url(self, path: List[str], module: dict):
        # Adapted from https://github.com/Blastd/UnimibKalturaResolver/blob/master/resolver.js

        escaped_path = escape_path(path + [f"{module['name']}.mp4"])
        file = Path(os.path.join(self.destdir, *escaped_path))
        if str(file) in self.videos:
            return

        print(f" - {' / '.join(path)}: Downloading video '{module['name']}.mp4'")

        # Load the video page
        r = self.browser.get(module["url"])
        r.raise_for_status()

        bs = BeautifulSoup(r.text, "html.parser")
        iframe_source = bs.find("iframe")["src"]

        # Get query parameters
        params = parse_qs(urlparse(iframe_source).query)
        source = params["source"][0]

        match = re.search(r"entryid/([^/]+)/", source)
        if match is None:
            print(f"   - {' / '.join(path)}: Could not find video ID in {source}")
            return
        entry_id = match.group(1)

        video_url = Urls.VIDEO.format(entry_id=entry_id)

        self.videos[str(file)] = video_url
        with open(self.videos_json, "w") as f:
            json.dump(self.videos, f, indent=2)

    def scrape_course(self, path: List[str], data: Union[dict, list]):
        if isinstance(data, list):
            with self.progress.counter(
                total=len(data), desc=pad_desc(path), unit="modules", leave=False
            ) as progress:
                for item in data:
                    self.scrape_course(path, item)
                    progress.update()
        else:
            if "modules" in data:
                self.scrape_course(
                    path + [data["name"]] if data["id"] != -1 and data["name"] else [],
                    data["modules"],
                )
            if "modname" in data:
                if data["modname"] in IGNORED_MODULES:
                    return

                match data["modname"]:
                    case "resource":
                        self.download_resources(path, data)
                    case "kalvidres":
                        self.save_kaltura_video_url(path, data)
                    case modname:
                        print(
                            f" - {' / '.join(path)}: Unknown module '{modname}' ({data.get('modplural')})"
                        )


def main():
    args = docopt(doc, version="unimib_login 0.1")
    try:
        username = args["--username"] or os.environ["UNIMIB_USERNAME"]
        password = args["--password"] or os.environ["UNIMIB_PASSWORD"]
    except KeyError:
        print("Username and/or password not provided")
        return

    cookie_jar = args["--cookiejar"]
    destdir = args["--destdir"]
    transfers = int(args["--transfers"])
    videos_json = args["--videos-json"]

    with BrowserSession(cookie_jar) as browser:
        token = browser.login(username, password)
        print(f"Logged in with wstoken {token}")

        moodle = Moodle(Urls.REST, token)
        with Scraper(
            browser, moodle, destdir, videos_json, transfers=transfers
        ) as scraper:
            print(
                f"Scraping from '{scraper.site_info.sitename}' as user {scraper.site_info.fullname}"
            )
            scraper.scrape()
