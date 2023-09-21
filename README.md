# UNIMIB Moodle Scraper

A command like tool to find and download all files (including lecture recording videos) from the Moodle platform of the
Università degli Studi di Milano-Bicocca.

The tool should be easily modifiable to work with other Moodle deployments that use Kaltura as video provider, but I
don't intend to support other deployments.

## Usage

```bash
python3 -m venv venv             # create a virtual environment
source venv/bin/activate         # activate the virtual environment
pip install -r requirements.txt  # install the dependencies

python -m unimib_scraper --help  # show the help
# for instance
python -m unimib_scraper -d ./downloads --transfers 25 -u n.lastname@campus.unimib.it -p PaSsWoRd
```

## Login issues

The login process is a bit fragile; if you get an error at the beginning, you can try copying the cookies from your
browser.

Open `cookies.json` and see which cookies are defined.

Go to https://elearning.unimib.it, log in. Open the developer tools, go to the
network tab, reload the page, and copy
the cookies from any request to `elearning.unimib.it` into the `cookies.json`
file. You don't need to copy all of them, just those that are present
in `cookies.json`.

## Credits

The Kaltura video URL resolver is based
on [Blastd/UnimibKalturaResolver](https://github.com/Blastd/UnimibKalturaResolver/),
which is also GPL-3.0 licensed.

## License

This project is licensed under the terms of the GNU General Public License v3.0.
