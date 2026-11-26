"""Registry of IRCC FCIP and RCIP communities.

18 unique communities participate in 20 community-program pairs: Sudbury and
Timmins are designated under both the Francophone Community Immigration Pilot
(FCIP) and the Rural Community Immigration Pilot (RCIP).

`municipalities` drives the geographic check in core.matcher: a job posting is
only linked to a designated employer when the job's city belongs to one of the
employer's designated communities.

NOTE: `portal_url` and `employer_source` are placeholders until verified in
Milestone 2 (employer ingestion). Municipality lists are best-effort and should
be checked against each community's official pilot boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Program(StrEnum):
    FCIP = "FCIP"
    RCIP = "RCIP"


class SourceType(StrEnum):
    HTML = "html"      # generic HTML table/list parser
    PDF = "pdf"        # generic pdfplumber parser
    CSV = "csv"        # hand-maintained seed file (data/seeds/<slug>.csv)
    CUSTOM = "custom"  # scrapers/employers/custom/<slug>.py


@dataclass(frozen=True)
class EmployerSource:
    type: SourceType
    url: str | None = None          # remote URL (html/pdf) or None for csv
    options: dict = field(default_factory=dict)  # parser hints (selectors, columns)


@dataclass(frozen=True)
class Community:
    slug: str
    name: str
    province: str                   # two-letter code
    programs: tuple[Program, ...]
    municipalities: tuple[str, ...]
    fsa_prefixes: tuple[str, ...] = ()   # postal-code forward sortation areas
    portal_url: str | None = None
    employer_sources: tuple[EmployerSource, ...] = ()

    @property
    def is_dual_program(self) -> bool:
        return len(self.programs) > 1


F, R = Program.FCIP, Program.RCIP

COMMUNITIES: tuple[Community, ...] = (
    # --- FCIP only -----------------------------------------------------------
    Community(
        slug="acadian-peninsula",
        name="Acadian Peninsula",
        province="NB",
        programs=(F,),
        municipalities=(
            "Caraquet", "Shippagan", "Tracadie", "Tracadie-Sheila", "Lamèque",
            "Bertrand", "Paquetville", "Saint-Isidore", "Neguac", "Grande-Anse",
            "Bas-Caraquet", "Saint-Léolin", "Le Goulet", "Miscou",
            "Hautes-Terres", "Île-de-Lamèque",
        ),
    ),
    Community(
        slug="superior-east",
        name="Superior East Region",
        province="ON",
        programs=(F,),
        municipalities=("Wawa", "Chapleau", "Dubreuilville", "White River"),
    ),
    Community(
        slug="st-pierre-jolys",
        name="St-Pierre-Jolys",
        province="MB",
        programs=(F,),
        municipalities=("St-Pierre-Jolys", "De Salaberry", "Saint-Malo", "Otterburne"),
    ),
    Community(
        slug="kelowna",
        name="Kelowna",
        province="BC",
        programs=(F,),
        municipalities=("Kelowna", "West Kelowna", "Lake Country", "Peachland"),
        portal_url="https://www.kelowna.ca",
    ),
    # --- Dual program --------------------------------------------------------
    Community(
        slug="sudbury",
        name="Greater Sudbury",
        province="ON",
        programs=(F, R),
        municipalities=(
            "Sudbury", "Greater Sudbury", "Grand Sudbury", "Lively", "Chelmsford",
            "Val Caron", "Hanmer", "Azilda", "Copper Cliff", "Garson",
            "Coniston", "Capreol", "Levack", "Dowling", "Val Thérèse",
            "Falconbridge", "Valley East", "Wahnapitae", "Whitefish",
        ),
        fsa_prefixes=("P3A", "P3B", "P3C", "P3E", "P3G", "P3L", "P3N", "P3P", "P3Y", "P0M"),
        portal_url="https://investsudbury.ca",
    ),
    Community(
        slug="timmins",
        name="Timmins",
        province="ON",
        programs=(F, R),
        municipalities=("Timmins", "Schumacher", "South Porcupine", "Porcupine"),
        fsa_prefixes=("P4N", "P4P", "P4R", "P0N"),
        portal_url="https://www.timmins.ca",
    ),
    # --- RCIP only -----------------------------------------------------------
    Community(
        slug="pictou-county",
        name="Pictou County",
        province="NS",
        programs=(R,),
        municipalities=(
            "New Glasgow", "Stellarton", "Westville", "Trenton", "Pictou",
            "River John", "Pictou Landing", "Thorburn", "Scotsburn", "Abercrombie",
        ),
    ),
    Community(
        slug="north-bay",
        name="North Bay",
        province="ON",
        programs=(R,),
        municipalities=(
            "North Bay", "Callander", "Powassan", "Mattawa", "East Ferris",
            "Bonfield", "Corbeil", "Astorville", "Sturgeon Falls",
        ),
        fsa_prefixes=("P1A", "P1B", "P1C", "P0H"),
        portal_url="https://www.northbay.ca",
    ),
    Community(
        slug="sault-ste-marie",
        name="Sault Ste. Marie",
        province="ON",
        programs=(R,),
        municipalities=("Sault Ste. Marie", "Prince Township", "Goulais River", "Echo Bay"),
        fsa_prefixes=("P6A", "P6B", "P6C"),
        portal_url="https://saultstemarie.ca",
    ),
    Community(
        slug="thunder-bay",
        name="Thunder Bay",
        province="ON",
        programs=(R,),
        municipalities=(
            "Thunder Bay", "Oliver Paipoonge", "Neebing", "Shuniah", "O'Connor",
            "Gillies", "Conmee", "Dorion", "Kakabeka Falls", "Murillo", "Rosslyn",
        ),
        fsa_prefixes=("P7A", "P7B", "P7C", "P7E", "P7G", "P7J", "P7K", "P0T"),
        portal_url="https://www.thunderbay.ca",
    ),
    Community(
        slug="steinbach",
        name="Steinbach",
        province="MB",
        programs=(R,),
        municipalities=(
            "Steinbach", "Hanover", "Mitchell", "Blumenort", "Kleefeld",
            "Grunthal", "La Broquerie", "New Bothwell",
        ),
        fsa_prefixes=("R5G", "R0A"),
        portal_url="https://www.steinbach.ca",
    ),
    Community(
        slug="altona-rhineland",
        name="Altona / Rhineland",
        province="MB",
        programs=(R,),
        municipalities=("Altona", "Rhineland", "Plum Coulee", "Gretna", "Rosenfeld"),
        fsa_prefixes=("R0G",),
        portal_url="https://www.altona.ca",
    ),
    Community(
        slug="brandon",
        name="Brandon",
        province="MB",
        programs=(R,),
        municipalities=("Brandon",),
        fsa_prefixes=("R7A", "R7B", "R7C"),
        portal_url="https://www.brandon.ca",
    ),
    Community(
        slug="moose-jaw",
        name="Moose Jaw",
        province="SK",
        programs=(R,),
        municipalities=("Moose Jaw",),
        fsa_prefixes=("S6H", "S6J", "S6K"),
        portal_url="https://moosejaw.ca",
    ),
    Community(
        slug="claresholm",
        name="Claresholm",
        province="AB",
        programs=(R,),
        municipalities=("Claresholm", "Granum", "Stavely", "Willow Creek"),
        fsa_prefixes=("T0L",),
        portal_url="https://www.claresholm.ca",
    ),
    Community(
        slug="west-kootenay",
        name="West Kootenay",
        province="BC",
        programs=(R,),
        municipalities=(
            "Trail", "Castlegar", "Rossland", "Nelson", "Warfield", "Fruitvale",
            "Montrose", "Salmo", "Kaslo", "Nakusp", "Slocan", "New Denver",
            "Silverton", "Genelle",
        ),
        fsa_prefixes=("V1R", "V1N", "V0G", "V1L"),
    ),
    Community(
        slug="north-okanagan-shuswap",
        name="North Okanagan Shuswap",
        province="BC",
        programs=(R,),
        municipalities=(
            "Vernon", "Salmon Arm", "Armstrong", "Enderby", "Spallumcheen",
            "Coldstream", "Lumby", "Sicamous", "Cherryville",
        ),
        fsa_prefixes=("V1B", "V1H", "V1T", "V0E", "V1E"),
        portal_url="https://www.vernon.ca",
    ),
    Community(
        slug="peace-liard",
        name="Peace Liard",
        province="BC",
        programs=(R,),
        municipalities=(
            "Fort St. John", "Dawson Creek", "Chetwynd", "Tumbler Ridge",
            "Pouce Coupe", "Taylor", "Hudson's Hope", "Fort Nelson",
        ),
        fsa_prefixes=("V1J", "V1G", "V0C"),
    ),
)

COMMUNITIES_BY_SLUG: dict[str, Community] = {c.slug: c for c in COMMUNITIES}


def get_community(slug: str) -> Community:
    try:
        return COMMUNITIES_BY_SLUG[slug]
    except KeyError as exc:
        raise KeyError(f"Unknown community slug: {slug!r}") from exc


def communities_for_program(program: Program | str) -> list[Community]:
    program = Program(program)
    return [c for c in COMMUNITIES if program in c.programs]


def community_program_pairs() -> list[tuple[Community, Program]]:
    """All 20 (community, program) designations."""
    return [(c, p) for c in COMMUNITIES for p in c.programs]
