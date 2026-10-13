from io import BytesIO
import unittest
from zipfile import ZipFile

from data.xlsx_matrix import iter_sheet_rows, sheet_paths


class XlsxMatrixTests(unittest.TestCase):
    def _workbook(self) -> BytesIO:
        content_types = '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'''
        workbook = '''<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="数据" sheetId="1" r:id="rId1"/></sheets></workbook>'''
        relationships = '''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet"/></Relationships>'''
        sheet = '''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>股票代码</t></is></c><c r="C1" t="inlineStr"><is><t>值</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>000001</t></is></c><c r="C2" t="n"><v>12.5</v></c></row></sheetData></worksheet>'''
        buffer = BytesIO()
        with ZipFile(buffer, "w") as archive:
            archive.writestr("[Content_Types].xml", content_types)
            archive.writestr("xl/workbook.xml", workbook)
            archive.writestr("xl/_rels/workbook.xml.rels", relationships)
            archive.writestr("xl/worksheets/sheet1.xml", sheet)
        buffer.seek(0)
        return buffer

    def test_streams_sparse_inline_and_numeric_cells(self) -> None:
        workbook = self._workbook()
        self.assertEqual(sheet_paths(workbook), {"数据": "xl/worksheets/sheet1.xml"})
        workbook.seek(0)
        self.assertEqual(list(iter_sheet_rows(workbook, "数据")), [["股票代码", None, "值"], ["000001", None, 12.5]])


if __name__ == "__main__":
    unittest.main()
