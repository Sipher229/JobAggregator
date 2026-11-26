"""Registry of IRCC FCIP and RCIP communities.

18 unique communities participate in 20 community-program pairs: Sudbury and
Timmins are designated under both the Francophone Community Immigration Pilot
(FCIP) and the Rural Community Immigration Pilot (RCIP).

Sources verified 2026-11-26 against IRCC's community pages and each community's
own site. NOTE: IRCC closes the RCIP pilot on 2027-01-01 and replaces it with the
permanent Rural Community Immigration Program (same 14 communities); employer
list URLs are likely to change in early 2027. Dated PDF filenames are resolved
at run time from the landing page (`link_text` / `link_href` options).

`municipalities` drives the geographic check in core.matcher: a job posting is
only linked to a designated employer when the job's city belongs to one of the
employer's designated communities.
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
    JSON = "json"      # JSON/AJAX endpoint
    CUSTOM = "custom"  # scrapers/employers/custom/<slug>.py


@dataclass(frozen=True)
class EmployerSource:
    type: SourceType
    url: str | None = None          # list URL, or landing page when link_text/link_href set
    # None = rows apply to every program the community has (or to the per-row
    # program column); set explicitly when a source lists only one program.
    program: Program | None = None
    options: dict = field(default_factory=dict)  # parser hints (see scrapers/employers/*)
    requires_js: bool = False       # fetch via Playwright instead of httpx
    notes: str = ""


@dataclass(frozen=True)
class Community:
    slug: str
    name: str
    province: str                   # two-letter code
    programs: tuple[Program, ...]
    municipalities: tuple[str, ...]
    fsa_prefixes: tuple[str, ...] = ()   # postal-code forward sortation areas
    portal_url: str | None = None        # official pilot page
    employer_sources: tuple[EmployerSource, ...] = ()
    notes: str = ""

    @property
    def is_dual_program(self) -> bool:
        return len(self.programs) > 1


F, R = Program.FCIP, Program.RCIP
HTML, PDF, JSON = SourceType.HTML, SourceType.PDF, SourceType.JSON

COMMUNITIES: tuple[Community, ...] = (
    # --- FCIP only -----------------------------------------------------------
    Community(
        slug="acadian-peninsula",
        name="Acadian Peninsula",
        province="NB",
        programs=(F,),
        municipalities=(
            "Caraquet", "Shippagan", "Tracadie", "Tracadie-Sheila", "Lamèque",
            "Bertrand", "Paquetville", "Saint-Isidore", "St-Isidore", "Neguac",
            "Grande-Anse", "Bas-Caraquet", "Saint-Léolin", "Le Goulet", "Miscou",
            "Hautes-Terres", "Île-de-Lamèque", "Bois-Gagnon", "Inkerman",
            "Pokemouche", "Sainte-Marie-Saint-Raphaël",
        ),
        portal_url="https://inspirepeninsuleacadienne.ca/programme-pilote-immigration-communautes-francophones/",
        employer_sources=(
            EmployerSource(
                PDF,
                url="https://inspirepeninsuleacadienne.ca/programme-pilote-immigration-communautes-francophones/",
                options={
                    "link_text": r"cliquez ici pour voir la liste des employeurs designes",
                    "mode": "lines",
                    # greedy name -> split on the LAST " - " (towns like "Grande-Anse")
                    "item_regex": r"^(?P<name>.+)\s+-\s+(?P<location>[^()]+?)"
                                  r"(?P<status>\s*\(ne recrute pas[^)]*\))?$",
                    "skip_patterns": [r"^commission de services", r"^liste des employeurs",
                                      r"^programme pilote", r"^les employeurs suivants",
                                      r"^pour la peninsule", r"^nb ?:"],
                },
                notes="French PDF, one 'Name - Town' per line; dated filename.",
            ),
        ),
    ),
    Community(
        slug="superior-east",
        name="Superior East Region",
        province="ON",
        programs=(F,),
        municipalities=("Wawa", "Chapleau", "Dubreuilville", "White River", "Hornepayne",
                        "Manitouwadge"),
        portal_url="https://superioreastcfdc.ca/superioreastcfdc.ca/index.php/fcip",
        employer_sources=(
            EmployerSource(
                HTML,
                url="https://superioreastcfdc.ca/superioreastcfdc.ca/index.php/fcip/fcip-employers",
                options={"container_heading": r"^designated employers$", "item_selector": "ul li"},
            ),
        ),
    ),
    Community(
        slug="st-pierre-jolys",
        name="St-Pierre-Jolys",
        province="MB",
        programs=(F,),
        municipalities=("St-Pierre-Jolys", "Saint-Pierre-Jolys", "St. Pierre Jolys",
                        "De Salaberry", "Saint-Malo", "St. Malo", "Otterburne"),
        portal_url="https://villagestpierrejolys.ca/p/francophone-communities-immigration-pilot-program",
        employer_sources=(
            EmployerSource(
                PDF,
                url="https://villagestpierrejolys.ca/p/francophone-communities-immigration-pilot-program",
                options={
                    "link_text": r"view the list of designated employers",
                    "mode": "columns",
                    "columns": {"priority_sector": [0, 235], "marker": [235, 258],
                                "name": [258, 9999]},
                    "row_key": "marker",
                    "start_after": r"^priority sectors",
                },
            ),
        ),
    ),
    Community(
        slug="kelowna",
        name="Kelowna",
        province="BC",
        programs=(F,),
        municipalities=("Kelowna", "West Kelowna", "Lake Country", "Peachland"),
        portal_url="https://www.sdecb.com/en/pilot-program/",
        employer_sources=(
            EmployerSource(
                HTML,
                url="https://www.sdecb.com/en/pilot-program/",
                options={"container": ".designated-employers__list", "item_selector": "li"},
            ),
        ),
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
        portal_url="https://investsudbury.ca/why-sudbury/newcomers/rcipfcip/",
        employer_sources=tuple(
            EmployerSource(
                HTML,
                url="https://investsudbury.ca/why-sudbury/newcomers/rcipfcip/",
                program=program,
                options={
                    "container": f".{program.lower()}_employer_boxes",
                    "row_selector": ".employer_box_container",
                    "fields": {"name": "h5", "website_url": "h5 a@href", "location": "p"},
                    "group_heading_selector": ".vc_tta-panel-body > h4",
                },
            )
            for program in (R, F)
        ),
    ),
    Community(
        slug="timmins",
        name="Timmins Region",
        province="ON",
        programs=(F, R),
        municipalities=(
            "Timmins", "Schumacher", "South Porcupine", "Porcupine", "Kirkland Lake",
            "Kapuskasing", "Hearst", "Iroquois Falls", "Cochrane", "Smooth Rock Falls",
            "Moonbeam", "Matheson", "Black River-Matheson", "Haileybury",
            "Temiskaming Shores", "New Liskeard", "Englehart", "Larder Lake",
        ),
        fsa_prefixes=("P4N", "P4P", "P4R", "P0N"),
        portal_url="https://timminsedc.com/immigration/",
        employer_sources=(
            EmployerSource(
                PDF,
                url="https://timminsedc.com/immigration/",
                options={
                    "link_href": r"Designated-Employer-List.*\.pdf$",
                    "mode": "columns",
                    "columns": {"priority_sector": [0, 170], "name": [170, 445],
                                "program": [445, 9999]},
                    "row_key": "program",
                    "start_after": r"^priority sector",
                },
                notes="One PDF for both pilots; 'Pilot Designated Under' column = RCIP/FCIP.",
            ),
        ),
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
            "Lyons Brook", "Merigomish", "Hopewell", "Durham",
        ),
        portal_url="https://pcrcip.ca/",
        employer_sources=(
            EmployerSource(
                PDF,
                url="https://pcrcip.ca/employers/",
                options={"link_href": r"PC-RCIP-Designated-Employers.*\.pdf$",
                         "skip_patterns": [r"^pictou county rcip"]},
            ),
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
        portal_url="https://nbrcip.ca/",
        employer_sources=(
            EmployerSource(
                JSON,
                url="https://nbrcip.ca/wp-admin/admin-ajax.php?action=load_monday_data",
                options={"fields": {"name": "name", "priority_sector": "color",
                                    "status": "status"}},
                notes="Data feed behind the DataTables list at /designated-employers/.",
            ),
        ),
    ),
    Community(
        slug="sault-ste-marie",
        name="Sault Ste. Marie",
        province="ON",
        programs=(R,),
        municipalities=("Sault Ste. Marie", "Prince Township", "Goulais River", "Echo Bay"),
        fsa_prefixes=("P6A", "P6B", "P6C"),
        portal_url="https://welcometossm.com/rcip/",
        employer_sources=(
            EmployerSource(
                HTML,
                url="https://welcometossm.com/designated-employers/",
                options={"container": '[data-id="2d26995f"]', "item_selector": "p",
                         "skip_patterns": [r"^en francaise$", r"^please ", r"^note",
                                           r"designation of employers"],
                         "max_name_length": 120},
            ),
        ),
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
        portal_url="https://gotothunderbay.ca/immigration/",
        notes="No public list as of 2026-11-26 (old page 404s). Use data/seeds/thunder-bay.csv.",
    ),
    Community(
        slug="steinbach",
        name="Steinbach",
        province="MB",
        programs=(R,),
        municipalities=(
            "Steinbach", "Hanover", "Mitchell", "Blumenort", "Kleefeld",
            "Grunthal", "La Broquerie", "New Bothwell", "Niverville", "Ste. Anne",
        ),
        fsa_prefixes=("R5G", "R0A"),
        portal_url="https://southeastmanitoba.com/rcip/",
        employer_sources=(
            EmployerSource(
                PDF,
                url="https://southeastmanitoba.com/rcip/",
                options={"link_href": r"RCIP-Designated-Employers\.pdf$",
                         "mode": "lines",
                         "start_after": r"list of designated employers",
                         "skip_patterns": [r"steinbachedc", r"@", r"\d{3}[-. )]+\d{3}[-. ]\d{4}",
                                           r"^\d+.*(street|avenue|ave|road|rd|st)\b"]},
            ),
        ),
    ),
    Community(
        slug="altona-rhineland",
        name="Altona / Rhineland",
        province="MB",
        programs=(R,),
        municipalities=("Altona", "Rhineland", "Plum Coulee", "Gretna", "Rosenfeld"),
        fsa_prefixes=("R0G",),
        portal_url="https://ared-rpga.com/immigration/rcip/",
        employer_sources=(
            EmployerSource(
                HTML,
                url="https://ared-rpga.com/immigration/rcip-employers/",
                options={"row_selector": "table.eael-data-table tbody tr",
                         "fields": {"name": "td"}},
            ),
        ),
    ),
    Community(
        slug="brandon",
        name="Brandon",
        province="MB",
        programs=(R,),
        municipalities=("Brandon",),
        fsa_prefixes=("R7A", "R7B", "R7C"),
        portal_url="https://economicdevelopmentbrandon.com/rcip/rcip",
        employer_sources=(
            EmployerSource(
                HTML,
                url="https://economicdevelopmentbrandon.com/rcip/rcip-list-of-designated-employers",
                options={"container": "div.com-content-article__body"},
                notes="Table 'Designated Employers | Notes'; notes hold hiring status.",
            ),
        ),
    ),
    Community(
        slug="moose-jaw",
        name="Moose Jaw",
        province="SK",
        programs=(R,),
        municipalities=("Moose Jaw", "Assiniboia", "Gravelbourg", "Chaplin", "Caronport",
                        "Mortlach", "Pense", "Rouleau"),
        fsa_prefixes=("S6H", "S6J", "S6K"),
        portal_url="https://rcip.mjchamber.com/",
        employer_sources=(
            EmployerSource(
                PDF,
                url="https://rcip.mjchamber.com/candidates/",
                options={"link_text": r"current designated employers",
                         "flow_columns": [[0, 320], [320, 9999]],
                         "start_after": r"designation later"},
                notes="LOW FIDELITY: names flow in 2 text columns with wraps indistinguishable "
                      "from new names. Curate data/seeds/moose-jaw.csv for accuracy.",
            ),
        ),
    ),
    Community(
        slug="claresholm",
        name="Claresholm",
        province="AB",
        programs=(R,),
        municipalities=("Claresholm", "Granum", "Stavely", "Willow Creek", "Nanton",
                        "Fort Macleod"),
        fsa_prefixes=("T0L",),
        portal_url="https://www.claresholm.ca/business/labour-resources/rural-community-immigration-pilot",
        employer_sources=(
            EmployerSource(
                HTML,
                url="https://www.claresholm.ca/business/labour-resources/rural-community-immigration-pilot",
                options={"container_heading": r"^designated employers$", "item_selector": "li",
                         "item_regex": r"^(?P<name>.+?)\s+-\s+(?P<priority_sector>\d+\s*,.+?)"
                                       r"\s+-\s+(?P<status>(?:not\s+)?hiring)\s*$"},
            ),
        ),
    ),
    Community(
        slug="west-kootenay",
        name="West Kootenay",
        province="BC",
        programs=(R,),
        municipalities=(
            "Trail", "Castlegar", "Rossland", "Nelson", "Warfield", "Fruitvale",
            "Montrose", "Salmo", "South Slocan", "Kaslo", "Nakusp", "Slocan",
            "New Denver", "Silverton", "Genelle", "Creston", "Grand Forks", "Greenwood",
            "Midway", "Rock Creek", "Riondel", "Ainsworth", "Crawford Bay", "Winlaw",
            "Christina Lake",
        ),
        fsa_prefixes=("V1R", "V1N", "V0G", "V1L", "V0B", "V0H"),
        portal_url="https://westkootenayimmigration.ca/",
        employer_sources=(
            EmployerSource(
                HTML,
                url="https://westkootenayimmigration.ca/designated-employers/",
                options={"row_selector": "table.has-fixed-layout tr", "fields": {"name": "td"},
                         "item_regex": r"^(?P<name>[^,]+?)(?:\s*,\s*(?P<location>.+))?$",
                         "group_heading_selector": "h4.c-accordion__title"},
                notes="Host may 403 aggressive clients; keep the per-host interval.",
            ),
        ),
    ),
    Community(
        slug="north-okanagan-shuswap",
        name="North Okanagan Shuswap",
        province="BC",
        programs=(R,),
        municipalities=(
            "Vernon", "Salmon Arm", "Armstrong", "Enderby", "Spallumcheen",
            "Coldstream", "Lavington", "Lumby", "Sicamous", "Cherryville", "Ashton Creek",
            "Anglemont", "Blind Bay", "Celista", "Deep Creek", "Falkland", "Grindrod",
            "Malakwa", "Scotch Creek", "Silver Creek", "Sorrento",
        ),
        fsa_prefixes=("V1B", "V1H", "V1T", "V0E", "V1E"),
        portal_url="https://rcipnorthokanaganshuswap.com/",
        employer_sources=(
            EmployerSource(
                PDF,
                url="https://rcipnorthokanaganshuswap.com/resources-and-policies/",
                options={"link_href": r"Designated-Employers-List.*\.pdf$",
                         "mode": "columns",
                         "columns": {"priority_sector": [0, 285], "name": [285, 9999]},
                         # every row repeats its sector; wrapped lines carry none or
                         # the label's tail ("and Government Services")
                         "row_key": "priority_sector", "row_key_pattern": r"^(?!and\b)",
                         "start_after": r"^priority sector",
                         "stop_at": r"^please note, the employers with an asterisk",
                         "skip_patterns": [r"^rcip nos", r"^designated employer list",
                                           r"^(january|february|march|april|may|june|july|august|"
                                           r"september|october|november|december) \d"]},
                notes="Trailing '*' = fast food / gas station (no longer eligible) -> flag.",
            ),
        ),
    ),
    Community(
        slug="peace-liard",
        name="Peace Liard",
        province="BC",
        programs=(R,),
        municipalities=(
            "Fort St. John", "Dawson Creek", "Chetwynd", "Tumbler Ridge",
            "Pouce Coupe", "Taylor", "Hudson's Hope", "Fort Nelson", "Charlie Lake",
        ),
        fsa_prefixes=("V1J", "V1G", "V0C"),
        portal_url="https://www.nebcimmigration.ca/",
        notes="No public list as of 2026-11-26 ('will be posted ... as soon as available'). "
              "Use data/seeds/peace-liard.csv.",
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
