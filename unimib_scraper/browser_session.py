import base64
import json
import random

import requests
import requests.utils
from bs4 import BeautifulSoup

from unimib_scraper import Urls

SSO_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class BrowserSession(requests.Session):
    def __init__(self, cookies_json_path: str):
        super(BrowserSession, self).__init__()
        self._cookies_json_path = cookies_json_path
        self.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/110.0"
            }
        )

        try:
            with open(cookies_json_path, "r") as f:
                cookies = requests.utils.cookiejar_from_dict(json.load(f))
                self.cookies.update(cookies)
        except FileNotFoundError:
            pass

    def __exit__(self, *a) -> None:
        with open(self._cookies_json_path, "w") as f:
            json.dump(requests.utils.dict_from_cookiejar(self.cookies), f)

        return super(BrowserSession, self).__exit__(*a)

    def request_skip_continue(self, *a, **kw) -> requests.Response:
        r = self.request(*a, **kw)
        r.raise_for_status()

        countdown = 100
        while True:
            if "Since your browser does not support JavaScript" not in r.text:
                return r

            countdown -= 1
            if countdown == 0:
                raise RuntimeError("Too many continue loops")

            domain = r.url.split("/")[2]
            bs4 = BeautifulSoup(r.text, "html.parser")
            form = bs4.find("form")
            action = (
                form["action"]
                if not form["action"].startswith("/")
                else f"https://{domain}{form['action']}"
            )
            print("New action:", action)

            r = self.request(
                form.get("method", "GET").upper(),
                action,
                data={
                    i["name"]: i.get("value", "")
                    for i in form.find_all("input")
                    if i.get("name")
                },
                headers=SSO_HEADERS,
                allow_redirects=True,
            )
            r.raise_for_status()

    def login(self, username: str, password: str):
        # Fetch Moodle login page
        # Logged in, all cookies are set. Fetch the mobile app token
        passport = str(random.random() * 900 + 100)
        r = self.get(Urls.MOBILE_TOKEN.format(passport=passport), allow_redirects=False)
        if not (300 <= r.status_code < 400):
            raise RuntimeError("Mobile app token request failed")

        location = r.headers["Location"]
        if not location.startswith("moodlemobile://"):
            r = self.request_skip_continue(
                "GET",
                location,
                headers=SSO_HEADERS,
                allow_redirects=True,
            )

            # Get the SAML link
            bs4 = BeautifulSoup(r.text, "html.parser")
            saml_link = bs4.find(id="unimibsaml_0").find("a")["href"]

            # Follow the SAML link; it will lead to a page with a form that auto-submits with JavaScript
            r = self.request_skip_continue(
                "GET",
                saml_link,
                allow_redirects=True,
                headers=SSO_HEADERS,
            )

            domain = r.url.split("/")[2]

            bs4 = BeautifulSoup(r.text, "html.parser")
            j_username = bs4.find("input", {"name": "j_username"})
            form = j_username.find_parent("form")
            action = (
                form["action"]
                if not form["action"].startswith("/")
                else f"https://{domain}{form['action']}"
            )

            def fill_in_form_input(input_el):
                match input_el["name"]:
                    case "j_username":
                        return username
                    case "j_password":
                        return password
                    case _:
                        return input_el.get("value", "")

            # We should have reached the actual login page. Submit the username/password form
            r = self.request_skip_continue(
                "POST",
                action,
                data={
                    i["name"]: fill_in_form_input(i)
                    for i in form.find_all("input")
                    if i.get("name")
                },
                headers=SSO_HEADERS,
                allow_redirects=True,
            )
            r.raise_for_status()

        redirect_url = r.headers["Location"]
        if not redirect_url.startswith("moodlemobile://"):
            raise RuntimeError("Invalid redirect URL")

        # The redirect URL contains the token
        token = redirect_url.split("token=")[1].split("&")[0]

        decoded = base64.b64decode(token)
        site_id, token, private_token = decoded.split(b":::")

        return token.decode("utf-8"), private_token.decode("utf-8")
