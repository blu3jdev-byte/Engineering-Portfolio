"""Site-wide settings. Edit this file to change your name, intro text, and links."""

# Where the site is published. GitHub Pages serves a repo site from /<repo-name>/.
# If you move to a custom domain, set BASE_PATH to "" and update SITE_URL.
SITE_URL = "https://blu3jdev-byte.github.io"
BASE_PATH = "/Portfolio"

NAME = "Jonah Pinon"
ROLE = "Mechanical engineer"
LEAD = (
    "Mechanical engineer. Each project here starts with the problem "
    "and ends with the solution I built."
)
DESCRIPTION = (
    "Engineering portfolio of Jonah Pinon, mechanical engineer: problem "
    "statements, solutions, videos, and publications."
)

LINKS = {
    "resume": "https://docs.google.com/document/d/1ppjNkIqzwy4K5ZzCrgCe4EcU4JIKTzpzRk4w2ndxws4/edit?usp=sharing",
    "linkedin": "https://www.linkedin.com/in/pinonj",
    "github": "https://github.com/blu3jdev-byte",
}

ACCOLADES = [
    {
        "label": "Modern Pentathlon national and world competitions",
        "href": "https://www.rdrnews.com/sports/local/roswell-pentathletes-take-gold/article_3cfd0db2-0d22-11ee-8004-3326319750c3.html",
    },
    {
        "label": "BSA Eagle Scout",
        "href": "https://www.rdrnews.com/news/local/conquistador-council-recognizes-2021-eagle-scouts/article_1481be8c-6b03-5d7a-95cc-3e30b951105f.html",
    },
    {
        "label": "National Science Bowl",
        "href": "https://www.rdrnews.com/news/local/nmmi-cadets-bound-for-national-science-bowl/article_cefd40ec-e2dd-11ee-85a8-7f85d953ae68.html",
    },
]

# How many projects the home page shows (featured projects come first).
HOME_PROJECT_COUNT = 4
