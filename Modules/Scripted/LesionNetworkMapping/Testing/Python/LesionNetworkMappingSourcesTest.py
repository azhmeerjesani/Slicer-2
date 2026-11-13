"""Tests of the literature sources and PRISMA counts (sources.py; L-51, addendum RW-12).

Parsers on sample files (nbib, RIS, BibTeX, CSV, pasted identifiers), dedupe (exact DOI / PMID merged, near-identical titles
offered), stages and undo, the metadata lookup with the network mocked (no test touches the network), cases linked to a
record, the PRISMA counts and the flow SVG (well-formed XML, no path, no script), and the files written.

Run without Slicer's GUI:

    cd Modules/Scripted/LesionNetworkMapping
    PythonSlicer -m unittest Testing/Python/LesionNetworkMappingSourcesTest.py -v
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if MODULE_DIR not in sys.path:
    sys.path.insert(0, MODULE_DIR)

from LesionNetworkMappingLib import share, sources
from LesionNetworkMappingLib.study import Study

NBIB = """PMID- 111111
OWN - NLM
TI  - Lesion network mapping of a rare symptom after a pontine
      infarct.
AU  - Smith J
FAU - Smith, John
AU  - Lee K
DP  - 2020 Jan
TA  - Brain
JT  - Brain : a journal of neurology
LID - 10.1093/brain/awz123 [doi]

PMID- 222222
TI  - A second paper.
AU  - Doe A
DP  - 2019
TA  - Neurology
AID - 10.1212/WNL.0000000000000999 [doi]
"""

RIS = """TY  - JOUR
TI  - Hallucinations after thalamic stroke
AU  - Garcia, Maria
AU  - Chen, Wei
PY  - 2018/03/01
JO  - Annals of Neurology
DO  - 10.1002/ana.25000
ER  -

TY  - JOUR
T1  - Mutism and the cerebellum
A1  - Novak, Petr
Y1  - 2021
JF  - Cortex
UR  - https://doi.org/10.1016/j.cortex.2021.01.001
ER  -
"""

BIBTEX = """@article{key1,
  title = {{Alien} hand after callosal lesion},
  author = {M{\\"u}ller, Hans and Rossi, Anna},
  journal = {Neuroimage},
  year = 2017,
  doi = {10.1016/j.neuroimage.2017.05.001},
}
@comment{ignored}
@article{key2,
  title = "Pure alexia",
  author = "Kim, Soo",
  year = "2015",
  pmid = {333333}
}
"""

CSV = "Title,DOI,PubMed ID,Year\nParkinsonism after a midbrain lesion,10.1000/PD.1,444444,2016\nNo identifiers here,,,2014\n"

# A Web of Science "Tab delimited file" export (savedrecs.txt: UTF-8 with a BOM, CRLF), a subset of its columns: the CR cell
# (cited references) holds DOIs that are not search hits, and the third record has a PMID but no DOI
WOS_HEADER = ["PT", "AU", "AF", "TI", "SO", "AB", "CR", "NR", "PY", "VL", "DI", "PM", "UT"]
WOS_ROWS = [
    ["J", "Boes, AD; Prasad, S", "Boes, Aaron D.; Prasad, Sashank", "Network localization of neurological symptoms", "BRAIN",
     "Lesions cause symptoms, in places; we map them.",
     "Fox MD, 2014, P NATL ACAD SCI USA, V111, P4367, DOI 10.1073/pnas.1405003111; Darby RR, 2017, BRAIN, V140, DOI 10.1093/brain/aww288",
     "2", "2015", "138", "10.1093/brain/awv228", "26254317", "WOS:000361930100023"],
    ["J", "Fox, MD", "Fox, Michael D.", "Mapping symptoms to brain networks", "NEW ENGLAND JOURNAL OF MEDICINE", "", "", "0", "2018",
     "379", "10.1056/NEJMra1706158", "30575457", "WOS:000453913000010"],
    ["J", "Darby, RR", "Darby, R. Ryan", "A record without a DOI", "CORTEX", "", "", "0", "2019", "", "", "31000123", "WOS:000000000000001"],
]
WOS = "\ufeff" + "\r\n".join("\t".join(row) for row in [WOS_HEADER] + WOS_ROWS) + "\r\n"

LOOKUP_CROSSREF = {"message": {"DOI": "10.1000/xyz", "title": ["Looked up title"], "author": [{"family": "Roe", "given": "Rita"}],
                               "issued": {"date-parts": [[2022, 5]]}, "container-title": ["Stroke"]}}
LOOKUP_ESUMMARY = {"result": {"uids": ["555555"], "555555": {"uid": "555555", "title": "From PubMed.", "pubdate": "2023 Feb",
                                                              "source": "Neurology", "authors": [{"name": "Poe E"}],
                                                              "articleids": [{"idtype": "pubmed", "value": "555555"}, {"idtype": "doi", "value": "10.1212/abc"}]}}}


class SourcesTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="lnm-sources-")
        self.study = Study.create(os.path.join(self.root, "study"), name="Lit")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # Parsers

    def test_nbib(self):
        records = sources.parse_nbib(NBIB)
        self.assertEqual(len(records), 2)
        first = records[0]
        self.assertEqual(first["pmid"], "111111")
        self.assertEqual(first["doi"], "10.1093/brain/awz123")
        self.assertEqual(first["title"], "Lesion network mapping of a rare symptom after a pontine infarct.")
        self.assertEqual(first["authors"], "Smith J; Lee K")
        self.assertEqual((first["year"], first["journal"]), ("2020", "Brain"))
        self.assertEqual(records[1]["doi"], "10.1212/wnl.0000000000000999")

    def test_nbib_pubmed_field_order_lists_each_author_once(self):
        # PubMed writes FAU before AU for every author: one author must not appear twice
        single = sources.parse_nbib("PMID- 3\nTI  - One author paper.\nFAU - Smith, John\nAU  - Smith J\nDP  - 2019\nTA  - Neurology\n")[0]
        self.assertEqual(single["authors"], "Smith, John")
        self.assertEqual(sources.citation({**single}), "Smith 2019, Neurology")
        three = (
            "PMID- 4\nTI  - Three authors.\nFAU - Boes, Aaron D\nAU  - Boes AD\nFAU - Prasad, Sashank\nAU  - Prasad S\n"
            "FAU - Liu, Hesheng\nAU  - Liu H\nDP  - 2015\nTA  - Brain\n"
        )
        self.assertEqual(sources.parse_nbib(three)[0]["authors"], "Boes, Aaron D; Prasad, Sashank; Liu, Hesheng")
        # AU only, and records where only some authors have an FAU line: the short names
        self.assertEqual(sources.parse_nbib("PMID- 5\nAU  - Smith J\nAU  - Lee K\n")[0]["authors"], "Smith J; Lee K")

    def test_ris(self):
        records = sources.parse_ris(RIS)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["authors"], "Garcia, Maria; Chen, Wei")
        self.assertEqual((records[0]["year"], records[0]["doi"]), ("2018", "10.1002/ana.25000"))
        self.assertEqual(records[1]["doi"], "10.1016/j.cortex.2021.01.001")
        self.assertEqual(records[1]["journal"], "Cortex")

    def test_bibtex(self):
        records = sources.parse_bibtex(BIBTEX)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["title"], "Alien hand after callosal lesion")
        self.assertEqual(records[0]["authors"], "M\u00fcller, Hans; Rossi, Anna")
        self.assertEqual((records[0]["year"], records[0]["journal"]), ("2017", "Neuroimage"))
        self.assertEqual(records[1]["pmid"], "333333")

    def test_bibtex_unclosed_entry_skipped(self):
        text = (
            "@article{a, title={First}, year=2020}\n"
            "@article{broken, title={Unclosed entry, author={Nobody}, year=2023\n\n"
            "@article{x3, title={Alexia without agraphia}, author={Rossi, Marco}, year={2016}}\n"
            "@article{q, title=\"Open quote, year=2001\n@article{z, title={Last}}\n"
        )
        records = sources.parse_bibtex(text)
        self.assertEqual([record["title"] for record in records], ["First", "Alexia without agraphia", "Last"])
        self.assertEqual(records[1]["year"], "2016")

    def test_bibtex_latex_accents_decoded(self):
        text = (
            "@article{a,\n  author = {M{\\\"u}ller, J{\\\"o}rg and Fern{\\'a}ndez, Ana and Sch\\\"on, B and {\\O}stergaard, K"
            " and Garc\\'{\\i}a, L and Fran\\c{c}ois, P and Dvo\\v{r}\\'ak, T},\n"
            "  title = {Lesion network of {\\\"a}phasia \\& {\\ss}tudy 100\\% of {X}},\n  journal = {Brain \\_ Mind},\n"
            "  year = {2020}\n}\n@article{b,\n  author = \"M\\\"uller, J\",\n  title = \"{\\'E}tude\"\n}\n"
        )
        first, second = sources.parse_bibtex(text)
        self.assertEqual(first["title"], "Lesion network of \u00e4phasia & \u00dftudy 100% of X")
        self.assertEqual(first["authors"], "M\u00fcller, J\u00f6rg; Fern\u00e1ndez, Ana; Sch\u00f6n, B; \u00d8stergaard, K; "
                         "Garc\u00eda, L; Fran\u00e7ois, P; Dvo\u0159\u00e1k, T")
        self.assertEqual(first["journal"], "Brain _ Mind")
        self.assertEqual((second["authors"], second["title"]), ("M\u00fcller, J", "\u00c9tude"))
        for record in (first, second):
            self.assertNotIn("\\", record["title"] + record["authors"])
        # the decoded title matches the same paper from another source
        self.assertEqual(sources.normalize_title(first["title"]), sources.normalize_title("Lesion network of \u00e4phasia & \u00dftudy 100% of X"))

    def test_csv(self):
        records = sources.parse_csv(CSV)
        self.assertEqual(len(records), 2)
        self.assertEqual((records[0]["doi"], records[0]["pmid"], records[0]["year"]), ("10.1000/pd.1", "444444", "2016"))
        with self.assertRaises(sources.SourcesError):
            sources.parse_csv("foo,bar\n1,2\n")

    def test_scopus_initials_authors_give_surname_ids_and_citations(self):
        text = (
            "Authors,Title,Year,Source title,DOI\n"
            '"Lefevre B.",T1,2020,Rev Neurol,10.1/a\n'
            '"Chen W., Patel R.",T2,2022,Brain,10.1/b\n'
            '"Smith J.A., Doe K.",T3,2021,X,10.1/c\n'
            '"Smith, John",T4,2019,Y,10.1/d\n'
        )
        _kind, records = sources.parse_text(text, "scopus.csv")
        got = [(sources._record_id_base(dict(r)), sources._citation(dict(r))) for r in records]
        self.assertEqual(
            got,
            [
                ("lefevre2020", "Lefevre 2020, Rev Neurol"),
                ("chen2022", "Chen et al. 2022, Brain"),
                ("smith2021", "Smith et al. 2021, X"),
                ("smith2019", "Smith 2019, Y"),
            ],
        )

    def test_identifiers(self):
        text = "https://doi.org/10.1000/ABC.1.\n12345678\nPMID: 987\n10.5555/x.y, not a doi\n\n10.1000/abc.1\n"
        records = sources.parse_identifiers(text)
        self.assertEqual([(r["doi"], r["pmid"]) for r in records], [("10.1000/abc.1", ""), ("", "12345678"), ("", "987"), ("10.5555/x.y", "")])

    def test_detect_format(self):
        self.assertEqual(sources.detect_format(NBIB), "nbib")
        self.assertEqual(sources.detect_format(RIS), "ris")
        self.assertEqual(sources.detect_format(BIBTEX), "bibtex")
        self.assertEqual(sources.detect_format(CSV, "x.csv"), "csv")
        self.assertEqual(sources.detect_format("10.1000/abc\n"), "identifiers")
        self.assertEqual(sources.parse_text(RIS, "a.txt")[0], "ris")

    def test_web_of_science_tab_delimited(self):
        # A WoS savedrecs.txt is a table (its records, with titles), not pasted identifiers (which would add the DOIs of the
        # cited references as records and lose the record without a DOI)
        self.assertEqual(sources.detect_format(WOS, "savedrecs.txt"), "csv")
        path = os.path.join(self.root, "savedrecs.txt")
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(WOS)
        for kind, records in (sources.parse_file(path), sources.parse_text(WOS, "savedrecs.tsv")):
            self.assertEqual(kind, "csv")
            self.assertEqual([(r["doi"], r["pmid"]) for r in records],
                             [("10.1093/brain/awv228", "26254317"), ("10.1056/nejmra1706158", "30575457"), ("", "31000123")])
            self.assertEqual(records[0]["title"], "Network localization of neurological symptoms")
            self.assertEqual((records[0]["authors"], records[0]["year"], records[0]["journal"]), ("Boes, AD; Prasad, S", "2015", "BRAIN"))
        # Pasted tab-separated identifiers (no header) stay identifiers
        self.assertEqual(sources.detect_format("10.1000/a\t10.1000/b\n"), "identifiers")
        self.assertEqual(sources.detect_format("DOI\t10.1000/a\n"), "identifiers")

    def test_parse_file(self):
        path = os.path.join(self.root, "export.nbib")
        with open(path, "w", encoding="utf-8") as f:
            f.write(NBIB)
        self.assertEqual(sources.parse_file(path)[0], "nbib")
        with self.assertRaises(sources.SourcesError):
            sources.parse_file(os.path.join(self.root, "missing.ris"))

    def test_windows_1252_and_utf16_files_are_decoded_without_replacement_characters(self):
        # EndNote and other Windows programs export Latin-1 / Windows-1252 text: it is decoded, never turned into U+FFFD
        ris = "TY  - JOUR\nTI  - Syndrome de Capgras apr\u00e8s un \u201cinfarctus\u201d \u2013 cas\nAU  - Lef\u00e8vre, H\u00e9l\u00e8ne\nPY  - 2017\nER  - \n"
        for name, data in (("latin1.ris", ris.replace("\u201c", '"').replace("\u201d", '"').replace("\u2013", "-").encode("latin-1")),
                           ("cp1252.ris", ris.encode("cp1252")), ("utf16.ris", ris.encode("utf-16")), ("utf8.ris", ris.encode("utf-8-sig"))):
            path = os.path.join(self.root, name)
            with open(path, "wb") as f:
                f.write(data)
            kind, records = sources.parse_file(path)
            self.assertEqual(kind, "ris", name)
            self.assertEqual(records[0]["authors"], "Lef\u00e8vre, H\u00e9l\u00e8ne", name)
            self.assertTrue(records[0]["title"].startswith("Syndrome de Capgras apr\u00e8s"), name)
            self.assertNotIn("\ufffd", sources.read_text(path), name)
        self.assertEqual(sources.decode_text("\u201cq\u201d".encode("cp1252")), "\u201cq\u201d")
        # Bytes that no supported encoding decodes are refused with a message, not imported with replacement characters
        with self.assertRaises(sources.SourcesError) as raised:
            sources.decode_text(b"\x81\x8d\x8f", "odd.ris")
        self.assertIn("odd.ris", str(raised.exception))
        self.assertIn("UTF-8", str(raised.exception))

    # Records and dedupe

    def test_add_and_ids(self):
        records = sources.Records.open(self.study)
        report = records.add(sources.parse_nbib(NBIB), origin="database")
        self.assertEqual(report["added"], ["smith2020", "doe2019"])
        report = records.add([sources.draft(authors="Smith, J", year="2020", title="Another paper entirely")])
        self.assertEqual(report["added"], ["smith2020b"])
        self.assertEqual(sources.Records(self.root).new_id({}), "rec")
        records.save()
        again = sources.Records.open(self.study)
        self.assertEqual(again.ids(), records.ids())
        self.assertEqual(again.get("smith2020")["stage"], "identified")
        with open(records.path, encoding="utf-8") as f:
            self.assertEqual(f.readline().rstrip("\n").split("\t"), list(sources.COLUMNS))

    def test_unreadable_stand_in_never_saves(self):
        """An empty table that stands in for an unreadable records.tsv refuses to save; prisma_counts uses it as given (BB-1)."""
        records = sources.Records.open(self.study)
        records.add(sources.parse_nbib(NBIB), origin="database")
        records.advance("smith2020")
        records.save()
        with open(records.path, "rb") as f:
            before = f.read()
        stand_in = sources.Records(self.study.path)
        stand_in.unreadable = "sources/records.tsv cannot be read: not UTF-8"
        stand_in.add([sources.draft(doi="10.9999/x")])
        with self.assertRaises(sources.SourcesError):
            stand_in.save()
        with open(records.path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertTrue(os.path.isfile(records.log_path))
        # An empty table is used as given, not replaced by the file on disk
        self.assertEqual(sources.prisma_counts(self.study, sources.Records(self.study.path))["screened"], 0)

    def test_unreadable_stage_log_is_reported_not_dropped(self):
        """A stage_log.tsv that cannot be read raises SourcesError: it is not dropped, so PRISMA does not move full-text
        exclusions to screening and the history is never written over (BB-1).
        """
        records = sources.Records.open(self.study)
        records.add([sources.draft(doi=f"10.9999/{n}") for n in "abc"])
        first, second, _third = records.ids()
        records.advance(first)
        records.advance(first)
        records.exclude(first, "No usable lesion imaging")
        records.save()
        counts = sources.prisma_counts(self.study, sources.Records.open(self.study))
        self.assertEqual((counts["excluded_screening"], counts["fulltext_excluded"]), (0, 1))
        with open(records.path, "rb") as f:
            table = f.read()
        for damage in (b"x\ty\tz\te1\te2\te3\n", b"\xff\xfe bad\n"):  # too many fields; not UTF-8 text
            with open(records.log_path, "rb") as f:
                good = f.read()
            with open(records.log_path, "ab") as f:
                f.write(damage)
            with open(records.log_path, "rb") as f:
                damaged = f.read()
            with self.assertRaises(sources.SourcesError) as raised:
                sources.Records.open(self.study)
            self.assertIn("stage_log.tsv", str(raised.exception))
            with self.assertRaises(sources.SourcesError):
                sources.prisma_counts(self.study)
            # The stand-in the Sources card uses refuses to save: neither file is written over
            stand_in = sources.Records(self.study.path)
            stand_in.unreadable = str(raised.exception)
            with self.assertRaises(sources.SourcesError) as refused:
                stand_in.save()
            self.assertIn("stage_log.tsv", str(refused.exception))
            with open(records.log_path, "rb") as f:
                self.assertEqual(f.read(), damaged)
            with open(records.path, "rb") as f:
                self.assertEqual(f.read(), table)
            with open(records.log_path, "wb") as f:
                f.write(good)
        # Once fixed, the history is back and the next change keeps it
        again = sources.Records.open(self.study)
        again.advance(second)
        again.save()
        counts = sources.prisma_counts(self.study, sources.Records.open(self.study))
        self.assertEqual((counts["excluded_screening"], counts["fulltext_excluded"]), (0, 1))
        self.assertEqual(counts["fulltext_excluded_reasons"], {"No usable lesion imaging": 1})

    def test_prisma_figure_says_1_case_in_the_singular(self):
        records = sources.Records.open(self.study)
        sources.import_text(self.study, records, "10.9999/one\n")
        rid = records.ids()[0]
        for _ in range(3):
            records.advance(rid)
        sources.add_cases(self.study, records, rid, 1)
        records.save()
        _counts, paths = sources.export_prisma(self.study, records)
        svg = open(paths[1]).read()
        self.assertIn(">1 case</text>", svg)
        self.assertNotIn("1 cases", svg)

    def test_exact_duplicates_merge(self):
        records = sources.Records.open(self.study)
        records.add([sources.draft(doi="10.1000/A", title="One")])
        report = records.add([sources.draft(doi="https://doi.org/10.1000/a", pmid="77", title="One (again)"), sources.draft(pmid="77")])
        self.assertEqual(report["added"], [])
        self.assertEqual(len(report["merged"]), 2)
        self.assertEqual(len(records), 1)
        self.assertEqual(records.rows[0]["pmid"], "77")  # filled from the duplicate
        self.assertEqual(records.rows[0]["title"], "One")

    def test_fuzzy_duplicates_are_offered_not_merged(self):
        records = sources.Records.open(self.study)
        records.add([sources.draft(title="Lesion network mapping of mutism after cerebellar stroke", year="2020", authors="A, B")])
        report = records.add([sources.draft(title="Lesion Network Mapping of Mutism After Cerebellar Stroke.", year="2020", authors="C, D"),
                              sources.draft(title="Lesion network mapping of mutism after cerebellar stroke", year="2019")])
        self.assertEqual(len(records), 3)
        self.assertEqual(len(report["offers"]), 1)
        keep, drop, ratio = report["offers"][0]
        self.assertGreaterEqual(ratio, sources.TITLE_RATIO)
        self.assertEqual(records.merge_offers(), report["offers"])
        records.merge(keep, drop)
        self.assertEqual(len(records), 2)
        self.assertEqual(records.merged_count(), 1)
        self.assertEqual(records.merge_offers(), [])

    def test_non_latin_titles_are_compared_in_their_own_script(self):
        # Titles are compared in any script: CJK, Cyrillic and Greek letters are kept (not reduced to their ASCII part)
        self.assertEqual(sources.normalize_title("Αφασία Μετά Αγγειακό"),
                         sources.normalize_title("ΑΦΑΣΙΑ ΜΕΤΑ ΑΓΓΕΙΑΚΟ"))  # noqa: RUF001
        self.assertEqual(sources.normalize_title("Étude, d'un cas."), "etude d un cas")
        self.assertEqual(sources.normalize_title("Zur Läsion"), sources.normalize_title("zur LASION"))
        self.assertEqual(sources.normalize_title("ガイド MRI"), "ガイド mri")  # kana keep their voicing marks
        first = "脑卒中后失语症的病灶网络映射：COVID-19 患者病例报告"  # noqa: RUF001
        other = "丘脑梗死后视幻觉 COVID-19 一例"
        russian = ("Картирование сети очагов "
                   "при афазии после инсульта")
        self.assertNotEqual(sources.normalize_title(first), sources.normalize_title(other))
        self.assertEqual(sources.normalize_title(russian), sources.normalize_title(russian + "."))
        records = sources.Records.open(self.study)
        records.add([sources.draft(title=first, year="2021"), sources.draft(title=other, year="2021"),
                     sources.draft(title=russian, year="2020"), sources.draft(title=russian + ".", year="2020")])
        self.assertEqual(len(records), 4)
        offers = records.merge_offers()
        self.assertEqual(len(offers), 1, offers)
        titles = {row["record_id"]: row["title"] for row in records.rows}
        self.assertEqual({titles[offers[0][0]], titles[offers[0][1]]}, {russian, russian + "."})
        self.assertGreaterEqual(offers[0][2], sources.TITLE_RATIO)

    # Stages

    def test_stages_and_undo(self):
        records = sources.Records.open(self.study)
        records.add([sources.draft(title="T1", authors="A, B", year="2020"), sources.draft(title="T2", authors="C, D", year="2020")])
        a, b = records.ids()
        self.assertEqual(records.advance(a), "screened")
        self.assertEqual(records.advance(a), "fulltext")
        self.assertEqual(records.advance(a), "included")
        self.assertEqual(records.advance(a), "included")
        with self.assertRaises(ValueError):
            records.exclude(b, "  ")
        with self.assertRaises(ValueError):
            records.set_stage(b, "bogus")
        records.exclude(b, "no imaging")
        self.assertEqual((records.get(b)["stage"], records.get(b)["exclusion_reason"]), ("excluded", "no imaging"))
        self.assertEqual(records.undo(b), "identified")
        self.assertEqual(records.get(b)["exclusion_reason"], "")
        self.assertEqual(records.undo(a), "fulltext")
        self.assertIsNone(records.undo(b))
        records.set_stage(a, "included")
        records.save()
        self.assertEqual(sources.Records.open(self.study).get(a)["stage"], "included")
        self.assertEqual(sources.Records.open(self.study).stage_counts()["included"], 1)

    # Lookup (mocked)

    def test_sici_dois_are_kept_whole(self):
        """A SICI-style DOI holds "<...>": it is not cut there (the cut DOI is not found and merges different papers)."""
        full = "10.1002/(sici)1099-1697(199803/04)7:2<111::aid-jsc318>3.0.co;2-q"
        for text in (full, "https://doi.org/" + full, "doi:" + full.upper(), "https://doi.org/10.1002/(SICI)1099-1697(199803/04)7:2%3C111::AID-JSC318%3E3.0.CO;2-Q"):
            self.assertEqual(sources.normalize_doi(text), full, text)
        self.assertEqual(sources.normalize_doi('<a href="https://doi.org/10.1000/xyz">10.1000/xyz</a>'), "10.1000/xyz")  # a tag is not part of it
        self.assertEqual([r["doi"] for r in sources.parse_identifiers(full + "\n")], [full])
        calls = []

        def get(url, params):
            calls.append(url)
            return LOOKUP_CROSSREF

        self.assertEqual(sources.lookup(doi=full, get=get)["doi"], full)
        self.assertEqual(calls, [sources.CROSSREF_URL + "10.1002/(sici)1099-1697(199803/04)7:2%3C111::aid-jsc318%3E3.0.co;2-q"])
        records = sources.Records.open(self.study)
        a = "10.1002/(sici)1099-1085(199805)12:6<821::aid-hyp668>3.0.co;2-y"
        b = "10.1002/(sici)1099-1085(199805)12:6<835::aid-hyp657>3.0.co;2-p"
        report = records.add([sources.draft(doi=a, title="Paper A on page 821"), sources.draft(doi=b, title="A different paper on page 835")])
        self.assertEqual((len(report["added"]), report["merged"], [r["doi"] for r in records.rows]), (2, [], [a, b]))

    def test_crossref_titles_are_plain_text_and_still_match(self):
        raw = "Neuroscience Discovers Power of &amp;ldquo;Lesion Network Mapping&amp;rdquo;"
        self.assertEqual(sources._crossref_record({"title": [raw]})["title"], "Neuroscience Discovers Power of \u201cLesion Network Mapping\u201d")
        fly = sources._crossref_record({"title": ["Ex Vivo Brain Imaging in<i>Drosophila</i>"], "container-title": ["Cold Spring <i>Harb</i> Protoc"]})
        self.assertEqual(fly["title"], "Ex Vivo Brain Imaging in Drosophila")
        self.assertEqual(fly["journal"], "Cold Spring Harb Protoc")
        self.assertEqual(sources.plain_text("H<sub>2</sub>O and a < b"), "H2O and a < b")
        self.assertEqual(sources.normalize_title("Ex Vivo Brain Imaging in<i>Drosophila</i>"), sources.normalize_title("Ex vivo brain imaging in Drosophila"))

    def test_lookup_crossref_and_esummary(self):
        calls = []

        def get(url, params):
            calls.append((url, params))
            return LOOKUP_CROSSREF if "crossref" in url else LOOKUP_ESUMMARY

        found = sources.lookup(doi="https://doi.org/10.1000/XYZ", get=get)
        self.assertEqual((found["title"], found["authors"], found["year"], found["journal"]), ("Looked up title", "Roe, Rita", "2022", "Stroke"))
        self.assertEqual(calls[0], (sources.CROSSREF_URL + "10.1000/xyz", None))
        found = sources.lookup(pmid="555555", get=get)
        self.assertEqual((found["doi"], found["year"], found["authors"]), ("10.1212/abc", "2023", "Poe E"))
        self.assertEqual(calls[1][1], {"db": "pubmed", "id": "555555", "retmode": "json"})  # only the identifier is sent

    def test_lookup_offline_is_safe(self):
        def offline(url, params):
            raise OSError("no network")

        with self.assertRaises(sources.SourcesError) as raised:
            sources.lookup(doi="10.1000/xyz", get=offline)
        self.assertIn("lookup failed", str(raised.exception))
        with self.assertRaises(sources.SourcesError):
            sources.lookup(get=offline)
        with self.assertRaises(sources.SourcesError):
            sources.lookup(pmid="1", get=lambda url, params: {"result": {"1": {"error": "cannot get document summary"}}})

    def test_lookup_http_status_is_not_a_connection_problem(self):
        class _Response:
            def __init__(self, status_code):
                self.status_code = status_code

        class _HTTPError(Exception):  # stands for requests.HTTPError (carries .response)
            def __init__(self, status_code):
                super().__init__(f"{status_code} Error")
                self.response = _Response(status_code)

        def failing(status):
            def get(url, params):
                raise _HTTPError(status)

            return get

        def message(**kwargs):
            with self.assertRaises(sources.SourcesError) as raised:
                sources.lookup(**kwargs)
            return str(raised.exception)

        self.assertEqual(message(doi="10.9999/does-not-exist", get=failing(404)), "Crossref has no record for this DOI")
        self.assertEqual(message(pmid="1", get=failing(404)), "PubMed has no record for this PMID")
        for status in (429, 503):
            text = message(doi="10.1000/xyz", get=failing(status))
            self.assertIn("busy or unavailable", text)
            self.assertNotIn("check the connection", text)
        text = message(doi="10.1000/xyz", get=failing(400))
        self.assertNotIn("check the connection", text)
        self.assertNotIn("_HTTPError", text)
        # no HTTP answer: still the connection text, without the exception class name
        text = message(doi="10.1000/xyz", get=lambda url, params: (_ for _ in ()).throw(OSError("no network")))
        self.assertIn("check the connection", text)
        self.assertNotIn("OSError", text)

    def test_fetch_fills_empty_fields_only(self):
        records = sources.Records.open(self.study)
        records.add([sources.draft(doi="10.1000/xyz", title="My own title")])
        rid = records.ids()[0]
        filled = sources.fetch_metadata(records, rid, get=lambda url, params: LOOKUP_CROSSREF)
        self.assertEqual(sorted(filled), ["authors", "journal", "year"])
        self.assertEqual(records.get(rid)["title"], "My own title")

    def test_fill_from_lookup_applies_a_draft_without_a_request(self):
        """The card runs ``lookup`` in a thread and applies the draft on the main thread with ``fill_from_lookup``."""
        records = sources.Records.open(self.study)
        records.add([sources.draft(doi="10.1000/xyz", title="My own title")])
        rid = records.ids()[0]
        found = sources.lookup(doi="10.1000/xyz", get=lambda url, params: LOOKUP_CROSSREF)
        filled = sources.fill_from_lookup(records, rid, found)
        self.assertEqual(sorted(filled), ["authors", "journal", "year"])
        self.assertEqual(records.get(rid)["title"], "My own title")
        self.assertEqual(sources.fill_from_lookup(records, rid, found), [])
        with self.assertRaises(KeyError):
            sources.fill_from_lookup(records, "nobody2000", found)

    def test_fetch_that_fills_another_records_doi_offers_the_exact_duplicate(self):
        """BB-1: a PMID-only record that a lookup gives the DOI of another record is offered as its duplicate."""
        records = sources.Records.open(self.study)
        records.add([sources.draft(doi="10.1000/xyz", title="Unrelated first title", year="2001"), sources.draft(pmid="123456", title="Other words entirely", year="1999")])
        first, second = records.ids()
        self.assertEqual(records.merge_offers(), [])
        found = sources.draft(doi="10.1000/XYZ", pmid="123456")
        self.assertIn("doi", sources.fill_from_lookup(records, second, found))
        self.assertEqual([offer[:2] for offer in records.merge_offers()], [(first, second)])
        self.assertTrue(records.same_identifier(first, second))

    def test_keep_both_is_stored_in_the_study(self):
        """BB-1: a "Keep both" decision is read back when the study is opened again, in either order, with no local path."""
        records = sources.Records.open(self.study)
        records.add([sources.draft(title="Same title here", year="2001", authors="Smith J"), sources.draft(title="Same title here.", year="2001", authors="Smith J")])
        records.save()
        first, second = records.ids()
        self.assertFalse(records.is_kept(first, second))
        records.keep_both(first, second)
        again = sources.Records.open(self.study)
        self.assertTrue(again.is_kept(first, second))
        self.assertTrue(again.is_kept(second, first))
        with open(again.kept_path, encoding="utf-8") as f:
            self.assertNotIn(os.sep + "tmp", f.read())

    def test_no_network_without_a_call(self):
        original = sources._http_json
        sources._http_json = lambda *a, **k: self.fail("network used")
        try:
            records = sources.Records.open(self.study)
            records.add(sources.parse_identifiers("10.1000/x\n123"))
            sources.prisma_counts(self.study, records)
            sources.prisma_flow_svg(sources.prisma_counts(self.study, records))
        finally:
            sources._http_json = original

    # Cases

    def test_add_cases(self):
        records = sources.Records.open(self.study)
        records.add(sources.parse_nbib(NBIB))
        created = sources.add_cases(self.study, records, "smith2020", 3)
        self.assertEqual(created, ["sub-smith202001", "sub-smith202002", "sub-smith202003"])
        row = self.study.subject("sub-smith202002")
        self.assertEqual((row["record_id"], row["doi"], row["pmid"]), ("smith2020", "10.1093/brain/awz123", "111111"))
        self.assertEqual((row["source_type"], row["group"]), ("case_report", "case"))
        self.assertEqual(row["citation"], "Smith et al. 2020, Brain")
        self.assertEqual(records.get("smith2020")["n_cases"], "3")
        self.assertEqual(sources.linked_subjects(self.study, "smith2020"), created)
        more = sources.add_cases(self.study, records, "smith2020", 2)
        self.assertEqual(more, ["sub-smith202004", "sub-smith202005"])
        with self.assertRaises(ValueError):
            sources.add_cases(self.study, records, "smith2020", 0)
        with self.assertRaises(KeyError):
            sources.add_cases(self.study, records, "nobody", 1)
        self.study.save()
        self.assertEqual(len(Study.open(self.study.path).subject_ids), 5)

    # PRISMA

    def _screened(self):
        records = sources.Records.open(self.study)
        sources.import_text(self.study, records, NBIB, "pubmed.nbib", database="PubMed", query="lesion network mapping")
        sources.import_text(self.study, records, RIS, "scopus.ris", database="Scopus", query="lnm")
        sources.import_text(self.study, records, NBIB, "pubmed-again.nbib", database="PubMed", query="again")  # two duplicates
        ids = records.ids()
        self.assertEqual(len(ids), 4)
        records.advance("smith2020")
        records.advance("smith2020")
        records.advance("smith2020")  # included
        records.get("smith2020")["n_cases"] = "4"
        records.exclude("doe2019", "wrong population")  # excluded at screening
        records.advance("garcia2018")
        records.exclude("garcia2018", "no imaging")  # excluded at full text
        return records

    def test_prisma_counts(self):
        records = self._screened()
        counts = sources.prisma_counts(self.study, records)
        self.assertEqual(counts["duplicates_removed"], 2)
        self.assertEqual(counts["identified"], 6)
        self.assertEqual(counts["screened"], 4)
        self.assertEqual(counts["excluded_screening"], 1)
        self.assertEqual(counts["fulltext_sought"], 2)  # smith2020 included, garcia2018 excluded after screening
        self.assertEqual(counts["fulltext_excluded"], 1)
        self.assertEqual(counts["fulltext_excluded_reasons"], {"no imaging": 1})
        self.assertEqual((counts["included"], counts["cases"], counts["unscreened"]), (1, 4, 1))
        self.assertEqual([entry["database"] for entry in counts["search"]], ["PubMed", "Scopus", "PubMed"])
        self.assertEqual(counts["search"][0]["query"], "lesion network mapping")
        self.assertEqual(counts["identified_by_origin"], {"database": 6, "citation": 0, "other": 0})
        # a manual merge counts as a removed duplicate and survives a reload
        records.add([sources.draft(title="Hallucinations after thalamic stroke", authors="X, Y", year="2018")])
        offers = records.merge_offers()
        self.assertEqual(len(offers), 1)
        records.merge(offers[0][0], offers[0][1])
        records.save()
        self.assertEqual(sources.prisma_counts(self.study)["duplicates_removed"], 3)

    def test_prisma_files_and_svg(self):
        records = self._screened()
        counts, paths = sources.export_prisma(self.study, records)
        self.assertEqual([os.path.relpath(p, self.study.path).replace(os.sep, "/") for p in paths],
                         ["derivatives/prisma/prisma_counts.json", "derivatives/prisma/prisma_flow.svg"])
        with open(paths[0], encoding="utf-8") as f:
            self.assertEqual(json.load(f)["identified"], counts["identified"])
        with open(paths[1], encoding="utf-8") as f:
            text = f.read()
        root = ET.fromstring(text)
        self.assertTrue(root.tag.endswith("svg"))
        shown = " ".join(element.text or "" for element in root.iter() if element.tag.endswith("text"))
        for needle in ("Records identified (n = 6)", "Duplicates removed", "Records screened (n = 4)", "no imaging: 1", "Studies included (n = 1)", "4 cases"):
            self.assertIn(needle, shown)
        self.assertNotIn("<script", text)
        self.assertNotIn(self.root, text)
        # special characters in a reason are escaped
        records.exclude("novak2021", "<b>&\"")
        ET.fromstring(sources.prisma_flow_svg(sources.prisma_counts(self.study, records)))

    def test_prisma_svg_wraps_long_reasons_and_uses_singular(self):
        counts = {"identified": 10, "fulltext_excluded": 90, "fulltext_excluded_reasons": {
            "No lesion masks or coordinates available from the authors": 30,
            "Non-focal or multifocal lesions (e.g. traumatic brain injury)": 12,
            "Lesion location not reported in sufficient detail": 40, "a": 5, "b": 4, "c": 3}}
        root = ET.fromstring(sources.prisma_flow_svg(counts))
        width = float(root.get("width"))
        texts = [element for element in root.iter() if element.tag.endswith("text") and element.get("x")]
        for element in texts:
            # a line is no wider than its 250 px box (estimate), and its centre plus half the width stays inside the figure
            half = sources._text_width(element.text or "", 12) / 2
            self.assertLessEqual(2 * half, sources._BOX_W - 16, element.text)
            self.assertLessEqual(float(element.get("x")) + half, width, element.text)
        shown = " ".join(element.text for element in texts)
        for needle in ("authors: 30", "injury): 12", "detail: 40", "2 other reasons"):
            self.assertIn(needle, shown)
        counts["fulltext_excluded_reasons"].pop("c")
        shown = " ".join(element.text for element in ET.fromstring(sources.prisma_flow_svg(counts)).iter() if element.tag.endswith("text"))
        self.assertIn("1 other reason", shown)
        self.assertNotIn("1 other reasons", shown)

    def test_prisma_svg_wraps_cjk_reasons(self):
        long_reason = "症状出现前已有其他脑损伤\uff0c或病灶不是单一的局灶性病变\uff0c无法确定责任病灶"
        counts = {"identified": 10, "fulltext_excluded": 8, "fulltext_excluded_reasons": {
            long_reason: 5, "症状出现前已有其他脑损伤或病灶": 3}}
        root = ET.fromstring(sources.prisma_flow_svg(counts))
        width = float(root.get("width"))
        texts = [element for element in root.iter() if element.tag.endswith("text") and element.get("x")]
        for element in texts:
            half = sources._text_width(element.text or "", 12) / 2
            self.assertLessEqual(2 * half, sources._BOX_W - 16, element.text)
            self.assertLessEqual(float(element.get("x")) + half, width, element.text)
        # a glyph is about one em wide, so no line holds more than (250 - 16) / 12 = 19 of them
        for element in texts:
            self.assertLessEqual(sum(1 for c in element.text if ord(c) > 0x2E80), 19, element.text)
        self.assertGreater(len([e for e in texts if "症" in e.text or "病" in e.text]), 2)

    def test_empty_study_svg(self):
        counts = sources.prisma_counts(self.study)
        self.assertEqual((counts["identified"], counts["included"]), (0, 0))
        ET.fromstring(sources.prisma_flow_svg(counts))

    def test_identified_by_origin_adds_up_to_identified(self):
        records = sources.Records.open(self.study)
        sources.import_text(self.study, records, NBIB, "pubmed.nbib", database="PubMed")
        sources.import_text(self.study, records, NBIB, "again.nbib", database="PubMed")  # duplicates of the first
        sources.import_text(self.study, records, NBIB, "cited.nbib", origin="citation", database="Citation searching")  # same records
        counts = sources.prisma_counts(self.study, records)
        self.assertGreater(counts["duplicates_removed"], 0)
        self.assertEqual(sum(counts["identified_by_origin"].values()), counts["identified"])
        self.assertGreater(counts["identified_by_origin"]["citation"], 0)

    def test_search_log_in_study_json(self):
        sources.add_search(self.study, "PubMed", "query", "2026-01-02", records=10, duplicates=1)
        self.study.save()
        self.assertEqual(Study.open(self.study.path).metadata["search"], [{"database": "PubMed", "query": "query", "date": "2026-01-02", "records": 10, "duplicates": 1}])

    # Export

    def test_records_exported_with_citations_only_and_no_paths(self):
        records = sources.Records.open(self.study)
        records.add(sources.parse_nbib(NBIB))
        records.get("smith2020")["notes"] = "private note"
        records.save()
        os.makedirs(records.attachments_folder("smith2020"))
        with open(os.path.join(records.attachments_folder("smith2020"), "fig-1.png"), "wb") as f:
            f.write(b"png")
        columns, _rows = share._records(self.study)
        self.assertNotIn("notes", columns)
        self.assertNotIn("stage", columns)
        with open(records.path, encoding="utf-8") as f:
            self.assertNotIn(self.root, f.read())

    def test_large_import_is_fast(self):
        import random
        import time

        rng = random.Random(3)
        vocab = ["".join(rng.choice("abcdefghijklmnop") for _ in range(rng.randint(3, 9))) for _ in range(800)]
        drafts = [
            {"title": " ".join(rng.choice(vocab) for _ in range(12)), "year": str(2000 + i % 15), "doi": f"10.1/x{i}"}
            for i in range(1500)
        ]
        drafts.append(dict(drafts[10], doi="10.1/other"))  # same title and year as record 10
        records = sources.Records(self.root)
        start = time.time()
        report = records.add(drafts)
        self.assertLess(time.time() - start, 15)
        self.assertEqual(len(report["added"]), 1501)
        self.assertEqual(len(report["offers"]), 1)
        start = time.time()
        self.assertEqual(len(records.merge_offers()), 1)
        self.assertLess(time.time() - start, 10)

    def test_chained_merges_all_count_as_duplicates(self):
        """Merging a record that had itself absorbed a duplicate keeps both merges in the PRISMA counts (order-independent)."""
        title = "Lesion network mapping of mutism after cerebellar stroke"
        for order in ("chain", "star"):
            with self.subTest(order=order):
                study = Study.create(os.path.join(self.root, order), name="Lit")
                records = sources.Records.open(study)
                records.add([sources.draft(title=title, year="2020"), sources.draft(title=title + ".", year="2020"),
                             sources.draft(title=title + "!", year="2020")])
                a, b, c = records.ids()
                records.set_stage(b, sources.SCREENED)
                records.merge(b, c)
                records.save()
                records = sources.Records.open(study)
                if order == "chain":
                    records.merge(a, b)  # b, which absorbed c, is dropped
                else:
                    records.merge(b, a)
                records.save()
                records = sources.Records.open(study)
                self.assertEqual(records.merged_count(), 2)
                self.assertEqual(len(records), 1)
                kept = records.ids()[0]
                self.assertTrue(all(entry["record_id"] == kept for entry in records.log))
                counts = sources.prisma_counts(study, records)
                self.assertEqual((counts["identified"], counts["duplicates_removed"], counts["screened"]), (3, 2, 1))
                # the stage line of the dropped record does not feed undo of the kept one
                if order == "chain":
                    self.assertIsNone(records.undo(kept))

    def test_merge_relinks_cases(self):
        records = sources.Records(self.root)
        records.add([{"title": "A", "year": "2020", "doi": "10.1/a"}, {"title": "B", "year": "2020", "doi": "10.1/b"}])
        a, b = records.ids()
        sources.add_cases(self.study, records, b, 2)
        records.merge(a, b)
        sources.relink_cases(self.study, a, b)
        self.assertEqual(len(sources.linked_subjects(self.study, a)), 2)
        self.assertEqual(sources.linked_subjects(self.study, b), [])

    def test_merge_keeps_cases_of_both_records_in_prisma(self):
        """Merging two included duplicates that both have cases keeps every case counted (n_cases and PRISMA cases)."""
        title = "Pure alexia after occipital stroke"
        for kept_cases, dropped_cases in ((2, 1), (1, 3)):
            with self.subTest(kept=kept_cases, dropped=dropped_cases):
                study = Study.create(os.path.join(self.root, f"both-{kept_cases}-{dropped_cases}"), name="Lit")
                records = sources.Records.open(study)
                records.add([sources.draft(title=title, year="2017"), sources.draft(title=title + ".", year="2017")])
                keep, drop = records.ids()
                for record_id, n in ((keep, kept_cases), (drop, dropped_cases)):
                    for _step in range(3):
                        records.advance(record_id)
                    sources.add_cases(study, records, record_id, n)
                before = sources.prisma_counts(study, records)
                records.merge(keep, drop)
                sources.relink_cases(study, keep, drop, records)
                linked = len(sources.linked_subjects(study, keep))
                self.assertEqual(linked, kept_cases + dropped_cases)
                self.assertEqual(records.get(keep)["n_cases"], str(linked))
                after = sources.prisma_counts(study, records)
                self.assertEqual((after["included"], after["cases"]), (before["included"] - 1, before["cases"]))
        # a larger count entered for the dropped copy (no linked cases) is kept
        records = sources.Records(os.path.join(self.root, "entered"))
        records.add([sources.draft(title=title, year="2017"), sources.draft(title=title + ".", year="2017")])
        keep, drop = records.ids()
        records.get(keep)["n_cases"], records.get(drop)["n_cases"] = "1", "4"
        records.merge(keep, drop)
        self.assertEqual(records.get(keep)["n_cases"], "4")

    def test_merge_keeps_inclusion_of_dropped_duplicate(self):
        """Merging an excluded record with its included duplicate keeps the inclusion: PRISMA included and cases stay."""
        title = "Pure alexia after occipital stroke"
        records = sources.Records.open(self.study)
        records.add([sources.draft(title=title, year="2017"), sources.draft(title=title + ".", year="2017")])
        keep, drop = records.ids()
        self.assertEqual([offer[:2] for offer in records.merge_offers()], [(keep, drop)])
        records.exclude(keep, "Not a case report")
        for _step in range(3):
            records.advance(drop)
        sources.add_cases(self.study, records, drop, 2)
        before = sources.prisma_counts(self.study, records)
        records.merge(keep, drop)
        sources.relink_cases(self.study, keep, drop)
        after = sources.prisma_counts(self.study, records)
        row = records.get(keep)
        self.assertEqual((row["stage"], row["exclusion_reason"], row["n_cases"]), (sources.INCLUDED, "", "2"))
        self.assertEqual((after["included"], after["cases"]), (before["included"], before["cases"]))
        self.assertEqual(after["duplicates_removed"], before["duplicates_removed"] + 1)
        # undo brings the exclusion back with its reason
        self.assertEqual(records.undo(keep), sources.EXCLUDED)
        self.assertEqual(records.get(keep)["exclusion_reason"], "Not a case report")
        # a duplicate that did not pass the stage the kept copy was excluded at leaves the exclusion as it was
        other = sources.Records(os.path.join(self.root, "other"))
        other.add([sources.draft(title=title, year="2017"), sources.draft(title=title + ".", year="2017")])
        keep, drop = other.ids()
        other.exclude(keep, "Not a case report")
        other.merge(keep, drop)
        self.assertEqual((other.get(keep)["stage"], other.get(keep)["exclusion_reason"]), (sources.EXCLUDED, "Not a case report"))


if __name__ == "__main__":
    unittest.main()
