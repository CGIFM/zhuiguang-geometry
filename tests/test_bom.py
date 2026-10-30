# -*- coding: utf-8 -*-
"""BOM 导出：装配体遍历结果的整理、数量乘法、列模板、xlsx/csv 落盘

真机要装 SolidWorks（在 167/174 上验）；这里把「驱动 SolidWorks」那一层换成
假数据，其余全是真代码——包括自己写的 xlsx 生成器。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from zgr import bom

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def sheet_text(path):
    """把 xlsx 里的第一个工作表读成二维字符串表（不借助任何第三方库）"""
    z = zipfile.ZipFile(path)
    root = ET.fromstring(z.read("xl/worksheets/sheet1.xml"))
    out = []
    for row in root.iter("{%s}row" % NS["m"]):
        cells = {}
        for c in row:
            ref = c.get("r")
            col = "".join(ch for ch in ref if ch.isalpha())
            t = c.get("t")
            if t == "inlineStr":
                is_ = c.find("m:is", NS)
                val = "".join(x.text or "" for x in is_.iter("{%s}t" % NS["m"])) if is_ is not None else ""
            else:
                v = c.find("m:v", NS)
                val = v.text if v is not None else ""
            cells[col] = val
        out.append(cells)
    return out


SAMPLE = [
    "ROOT|总装|C:\\项目\\总装.sldasm",
    "ASM|0|1.0|0.1|左组件|C:\\项目\\左组件.sldasm",
    "PART|1|2.0|0.1.1|螺丝-1|C:\\项目\\螺丝.sldprt|GJ-001|内六角螺丝|不锈钢|发黑|0.02|标准件|东明|标准件|个",
    "PART|1|2.0|0.1.2|底板-1|C:\\项目\\底板.sldprt|GJ-002|底板|6061||0.5||||件",
    "PART|0|1.0|0.2|底板-2|C:\\项目\\底板.sldprt|GJ-002|底板|6061||0.5||自制||件",
    "PART|0|1.0|0.3|外购件-1|C:\\项目\\外购件.sldprt||外购件|45#|||",
    "DONE",
]


class ParseTest(unittest.TestCase):
    def test_quantity_multiplied_through_subassembly(self):
        root, rows = bom.parse_lines(SAMPLE)
        self.assertEqual(root, "总装")
        parts = [r for r in rows if r["kind"] == "part"]
        by_path = {r["path"]: r for r in parts}
        # 子装配装了 2 份，里面每颗螺丝累计 2 × 1 = 2
        self.assertEqual(by_path["0.1.1"]["qty"], 2.0)
        self.assertEqual(by_path["0.2"]["qty"], 1.0)

    def test_merge_by_code_sums_quantities(self):
        _root, rows = bom.parse_lines(SAMPLE)
        _cols, table = bom.build_table(rows, merge_by="code")
        by_code = {r["code"]: r for r in table}
        # 子装配里 2 份 + 顶层 1 份 = 3 份，同图号合成一行
        self.assertEqual(by_code["GJ-002"]["qty"], 3, "同图号的底板要合成一行")
        self.assertAlmostEqual(by_code["GJ-002"]["total_mass"], 1.5, places=3)
        self.assertEqual(by_code["GJ-001"]["qty"], 2)
        self.assertEqual(by_code["GJ-001"]["surface"], "发黑")

    def test_brand_type_unit_are_kept(self):
        _root, rows = bom.parse_lines(SAMPLE)
        _cols, table = bom.build_table(rows, merge_by="code")
        by_code = {r["code"]: r for r in table}
        self.assertEqual(by_code["GJ-001"]["brand"], "东明")
        self.assertEqual(by_code["GJ-001"]["ptype"], "标准件")
        self.assertEqual(by_code["GJ-001"]["unit"], "个")
        # 同一图号合并时，同一行里空着的品牌由另一条实例补上
        self.assertEqual(by_code["GJ-002"]["brand"], "自制")

    def test_ican_columns_are_the_default(self):
        heads = [t for t, _f in bom.DEFAULT_COLUMNS]
        self.assertEqual(heads, ["序号", "图号", "零件名称", "材质", "表面处理",
                                 "品牌", "零件类型", "数量", "单位"])
        self.assertEqual([f for _t, f in bom.DEFAULT_COLUMNS],
                         [f for _t, f in bom.ICAN_COLUMNS])

    def test_without_code_falls_back_to_filename(self):
        _root, rows = bom.parse_lines(SAMPLE)
        _cols, table = bom.build_table(rows, merge_by="code")
        names = [r["name"] for r in table]
        self.assertIn("外购件", names, "没有图号也要出现在清单里，不能悄悄丢掉")

    def test_merge_none_keeps_every_instance_row(self):
        _root, rows = bom.parse_lines(SAMPLE)
        _cols, table = bom.build_table(rows, merge_by="none")
        self.assertEqual(len(table), 4)

    def test_include_children_false_only_top_level(self):
        _root, rows = bom.parse_lines(SAMPLE)
        _cols, table = bom.build_table(rows, include_children=False,
                                       merge_by="none")
        self.assertEqual(len(table), 2, "只要顶层时不该出现子装配里的零件")

    def test_fatal_line_raises(self):
        with self.assertRaises(bom.BomError):
            bom.parse_lines(["FATAL open 13 类型不匹配"])

    def test_missing_components_are_reported(self):
        root, rows = bom.parse_lines(SAMPLE + ["MISSING|1|0.1.9"])
        missing = [r for r in rows if r["kind"] == "missing"]
        self.assertEqual(len(missing), 1)
        self.assertEqual(missing[0]["path"], "0.1.9")
        # 缺件不能混进物料行
        _cols, table = bom.build_table(rows)
        self.assertEqual(len(table), 3)

    def test_only_missing_parts_gives_a_clear_error(self):
        tmp = tempfile.mkdtemp(prefix="zgr-bom-m-")
        self.addCleanup(shutil.rmtree, tmp, True)
        asm = os.path.join(tmp, "总装.sldasm")
        with open(asm, "wb") as f:
            f.write(b"fake")
        with mock.patch.object(bom, "collect", return_value=(
                "总装", bom.parse_lines(["MISSING|0|0.1", "MISSING|0|0.2"])[1])):
            with self.assertRaises(bom.BomError) as e:
                bom.export(asm, os.path.join(tmp, "out"))
        self.assertIn("引用丢失", str(e.exception))

    def test_missing_components_carry_their_names(self):
        """缺件要说出是哪几个——用户要的不是「2 个」，是「底板-1、侧板-2」"""
        _r, rows = bom.parse_lines(["MISSING|0|0.1|底板-1", "MISSING|0|0.2|侧板-2",
                                    "MISSING|0|0.3"])
        miss = [r for r in rows if r["kind"] == "missing"]
        self.assertEqual([m["name"] for m in miss], ["底板-1", "侧板-2", ""])
        self.assertEqual(bom._name_tail([m["name"] for m in miss]),
                         "：底板-1、侧板-2")
        self.assertEqual(bom._name_tail([]), "")
        self.assertEqual(bom._name_tail(["a", "b", "c"], 2), "：a、b等 3 个")

    def test_clear_error_names_the_parts(self):
        tmp = tempfile.mkdtemp(prefix="zgr-bom-mn-")
        self.addCleanup(shutil.rmtree, tmp, True)
        asm = os.path.join(tmp, "总装.sldasm")
        with open(asm, "wb") as f:
            f.write(b"fake")
        with mock.patch.object(bom, "collect", return_value=(
                "总装", bom.parse_lines(["MISSING|0|0.1|底板-1", "MISSING|0|0.2|侧板-2"])[1])):
            with self.assertRaises(bom.BomError) as e:
                bom.export(asm, os.path.join(tmp, "out"))
        self.assertIn("底板-1", str(e.exception))
        self.assertIn("侧板-2", str(e.exception))

    def test_missing_names_in_result(self):
        tmp = tempfile.mkdtemp(prefix="zgr-bom-mr-")
        self.addCleanup(shutil.rmtree, tmp, True)
        asm = os.path.join(tmp, "总装.sldasm")
        with open(asm, "wb") as f:
            f.write(b"fake")
        with mock.patch.object(bom, "collect", return_value=(
                "总装", bom.parse_lines(SAMPLE + ["MISSING|1|0.1.9|缺件-A"])[1])):
            res = bom.export(asm, os.path.join(tmp, "B5"))
        self.assertEqual(res["missing"], 1)
        self.assertIn("缺件-A", res["missing_names"])

    def test_serial_numbers_are_sequential(self):
        _root, rows = bom.parse_lines(SAMPLE)
        _cols, table = bom.build_table(rows)
        self.assertEqual([r["no"] for r in table], list(range(1, len(table) + 1)))


class XlsxTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-bom-x-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_a_readable_sheet(self):
        _root, rows = bom.parse_lines(SAMPLE)
        cols, table = bom.build_table(rows)
        p = os.path.join(self.tmp, "bom.xlsx")
        bom.write_xlsx(p, cols, table, title="总装 物料清单")
        grid = sheet_text(p)
        self.assertIn("总装 物料清单",
                      "".join(grid[0].values()))
        header = [grid[1].get(c, "") for c in "ABCDEFGHI"]
        self.assertEqual(header, ["序号", "图号", "零件名称", "材质",
                                  "表面处理", "品牌", "零件类型", "数量", "单位"])
        # 第一行数据：序号是数字
        self.assertEqual(grid[2].get("A"), "1")
        self.assertEqual(len(grid), 2 + len(table))

    def test_prelude_rows_sit_above_the_header(self):
        p = os.path.join(self.tmp, "h.xlsx")
        bom.write_xlsx(p, [("序号", "no")], [{"no": 1}],
                       title=bom.ICAN_TITLE, prelude=bom.ICAN_HEADER_ROWS)
        grid = sheet_text(p)
        self.assertEqual(grid[0].get("A"), bom.ICAN_TITLE)
        self.assertEqual(grid[1].get("A"), "项目名称")
        self.assertEqual(grid[1].get("G"), "下单时间")
        self.assertEqual(grid[3].get("A"), "序号")      # 表头在第 4 行
        self.assertEqual(grid[4].get("A"), "1")

    def test_escapes_special_characters(self):
        p = os.path.join(self.tmp, "e.xlsx")
        bom.write_xlsx(p, [("名", "name")], [{"name": 'A&B <C> "D"'}])
        grid = sheet_text(p)
        self.assertEqual(grid[1].get("A"), 'A&B <C> "D"')

    def test_empty_cells_are_omitted_but_layout_holds(self):
        p = os.path.join(self.tmp, "n.xlsx")
        bom.write_xlsx(p, [("a", "a"), ("b", "b")], [{"a": 1, "b": ""}])
        grid = sheet_text(p)
        self.assertEqual(grid[1].get("A"), "1")
        self.assertNotIn("B", grid[1])

    def test_csv_is_utf8_bom(self):
        p = os.path.join(self.tmp, "b.csv")
        bom.write_csv(p, [("图号", "code")], [{"code": "GJ-001"}])
        with open(p, "rb") as f:
            self.assertTrue(f.read(3) == b"\xef\xbb\xbf", "Excel 直接双击要能认中文")
        with open(p, encoding="utf-8-sig") as f:
            self.assertIn("GJ-001", f.read())


class TemplateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-bom-t-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_template_columns_override_defaults(self):
        with open(os.path.join(self.tmp, bom.TEMPLATE_NAME), "w",
                  encoding="utf-8") as f:
            json.dump({"columns": [["序号", "no"], ["零件号", "code"],
                                   ["零件名称", "name"], ["数量", "qty"]]}, f)
        tpl = bom.load_template(self.tmp)
        self.assertEqual([t for t, _f in tpl["columns"]],
                         ["序号", "零件号", "零件名称", "数量"])
        self.assertEqual(tpl["header"], [], "没写 header 就不该凭空插表头块")

    def test_template_title_and_header_block(self):
        with open(os.path.join(self.tmp, bom.TEMPLATE_NAME), "w",
                  encoding="utf-8") as f:
            json.dump({"title": "嘉立创Ican工具箱",
                       "header": [["项目名称", "", "", "", "", "", "下单时间"]],
                       "columns": [["序号", "no"], ["数量", "qty"]]}, f)
        tpl = bom.load_template(self.tmp)
        self.assertEqual(tpl["title"], "嘉立创Ican工具箱")
        self.assertEqual(tpl["header"][0][0], "项目名称")
        self.assertEqual(tpl["header"][0][6], "下单时间")

    def test_broken_template_falls_back(self):
        with open(os.path.join(self.tmp, bom.TEMPLATE_NAME), "w",
                  encoding="utf-8") as f:
            f.write("{ 不是 JSON")
        self.assertIsNone(bom.load_template(self.tmp))

    def test_template_with_bom_is_read(self):
        with open(os.path.join(self.tmp, bom.TEMPLATE_NAME), "w",
                  encoding="utf-8-sig") as f:
            json.dump([{"title": "序号", "field": "no"}], f)
        tpl = bom.load_template(self.tmp)
        self.assertEqual(tpl["columns"], [("序号", "no")])


class ExportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-bom-e-")
        self.asm = os.path.join(self.tmp, "总装.sldasm")
        with open(self.asm, "wb") as f:
            f.write(b"fake")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_export_writes_both_files_and_reports(self):
        dest = os.path.join(self.tmp, "BOM")
        with mock.patch.object(bom, "collect", return_value=("总装",
                                                             bom.parse_lines(SAMPLE)[1])):
            res = bom.export(self.asm, dest)
        self.assertTrue(os.path.isfile(res["xlsx"]))
        self.assertTrue(os.path.isfile(res["csv"]))
        self.assertEqual(res["files"], len(res["rows"]))
        self.assertGreaterEqual(res["total_qty"], 4)
        self.assertTrue(os.path.basename(res["xlsx"]).startswith("总装-BOM-"))

    def test_export_refuses_non_assembly(self):
        p = os.path.join(self.tmp, "零件.sldprt")
        open(p, "wb").close()
        with self.assertRaises(bom.BomError) as e:
            bom.collect(p)
        self.assertIn(".sldasm", str(e.exception))

    def test_export_without_parts_raises(self):
        with mock.patch.object(bom, "collect", return_value=("总装", [])):
            with self.assertRaises(bom.BomError):
                bom.export(self.asm, os.path.join(self.tmp, "BOM2"))


class SnapshotTest(unittest.TestCase):
    """和上一次生成比：多了哪些料、少了哪些、数量变了多少"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zgr-bom-s-")
        self.asm = os.path.join(self.tmp, "总装.sldasm")
        with open(self.asm, "wb") as f:
            f.write(b"fake")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rows(self, items):
        return [{"no": i, "code": c, "name": n, "qty": q, "unit": "个", "_key": c}
                for i, (c, n, q) in enumerate(items, 1)]

    def test_first_run_has_no_diff_but_saves_snapshot(self):
        snap = bom.make_snapshot(self._rows([("A-1", "底板", 2)]), self.asm, self.tmp)
        bom.save_snapshot(self.tmp, self.asm, snap)
        p = bom._snap_file(self.tmp, self.asm)
        self.assertTrue(os.path.isfile(p))
        self.assertTrue(p.startswith(os.path.join(self.tmp, ".zgr")))
        self.assertEqual(len(bom.load_snapshots(self.tmp, self.asm)), 1)

    def test_diff_added_removed_changed(self):
        old = {"time": 1, "rows": [
            {"key": "A-1", "code": "A-1", "name": "底板", "qty": 2},
            {"key": "B-2", "code": "B-2", "name": "侧板", "qty": 1},
        ]}
        new = {"time": 2, "rows": [
            {"key": "A-1", "code": "A-1", "name": "底板", "qty": 4},   # 数量变了
            {"key": "C-3", "code": "C-3", "name": "盖板", "qty": 1},   # 新增
        ]}
        d = bom.diff_snapshots(old, new)
        self.assertEqual([r["code"] for r in d["added"]], ["C-3"])
        self.assertEqual([r["code"] for r in d["removed"]], ["B-2"])
        self.assertEqual(len(d["changed"]), 1)
        self.assertEqual((d["changed"][0]["old"], d["changed"][0]["new"]), (2, 4))
        self.assertEqual(d["old_time"], 1)

    def test_no_change(self):
        snap = {"time": 1, "rows": [{"key": "A", "code": "A", "name": "x", "qty": 1}]}
        d = bom.diff_snapshots(snap, snap)
        self.assertEqual((len(d["added"]), len(d["removed"]), len(d["changed"])), (0, 0, 0))

    def test_keeps_only_last_20(self):
        for i in range(25):
            bom.save_snapshot(self.tmp, self.asm,
                              {"time": i, "rows": [{"key": "A", "qty": i}]})
        snaps = bom.load_snapshots(self.tmp, self.asm)
        self.assertEqual(len(snaps), bom.SNAPSHOT_KEEP)
        self.assertEqual(snaps[-1]["time"], 24)          # 留的是最新的
        self.assertEqual(snaps[0]["time"], 5)

    def test_export_reports_diff_against_previous(self):
        dest = os.path.join(self.tmp, "BOM")
        with mock.patch.object(bom, "collect",
                               return_value=("总装", bom.parse_lines(SAMPLE)[1])):
            first = bom.export(self.asm, dest, project_root=self.tmp)
        self.assertIsNone(first["diff"])                 # 第一次没有上一次可比
        with mock.patch.object(bom, "collect",
                               return_value=("总装", bom.parse_lines(SAMPLE)[1])):
            second = bom.export(self.asm, dest, project_root=self.tmp)
        self.assertIsNotNone(second["diff"])
        d = second["diff"]
        self.assertEqual((len(d["added"]), len(d["removed"]), len(d["changed"])), (0, 0, 0))

    def test_merge_mode_change_skips_diff(self):
        """这次按图号、上次按名称：行键对不上，不能硬比成「全新增」"""
        dest = os.path.join(self.tmp, "BOM")
        with mock.patch.object(bom, "collect",
                               return_value=("总装", bom.parse_lines(SAMPLE)[1])):
            bom.export(self.asm, dest, project_root=self.tmp, merge_by="code")
        with mock.patch.object(bom, "collect",
                               return_value=("总装", bom.parse_lines(SAMPLE)[1])):
            res = bom.export(self.asm, dest, project_root=self.tmp, merge_by="none")
        self.assertIsNone(res["diff"])
        self.assertIn("口径不一样", res["diff_skip"])
        # 口径一致时照常比
        with mock.patch.object(bom, "collect",
                               return_value=("总装", bom.parse_lines(SAMPLE)[1])):
            res2 = bom.export(self.asm, dest, project_root=self.tmp, merge_by="none")
        self.assertIsNotNone(res2["diff"])
        self.assertEqual(res2["diff_skip"], "")

    def test_mode_recorded_in_snapshot(self):
        snap = bom.make_snapshot([], self.asm, self.tmp,
                                 {"merge_by": "code", "include_children": False})
        self.assertEqual(snap["mode"]["merge_by"], "code")
        self.assertFalse(snap["mode"]["include_children"])
        self.assertIn("只要顶层", bom._mode_name(snap["mode"]))

    def test_summarize_missing_attrs(self):
        rows = [
            {"code": "A-1", "name": "底板", "material": "45#", "brand": "自制",
             "surface": "发黑", "file": "零件/底板.sldprt"},
            {"code": "", "name": "侧板", "material": " ", "brand": "",
             "surface": "发黑", "file": "零件/侧板.sldprt"},
        ]
        miss = {m["field"]: m for m in bom.summarize_missing(rows)}
        self.assertEqual(miss["code"]["n"], 1)
        self.assertEqual(miss["code"]["label"], "图号")
        self.assertEqual(miss["code"]["files"], ["零件/侧板.sldprt"])
        self.assertEqual(miss["material"]["n"], 1)      # 只有空格也算没填
        self.assertEqual(miss["brand"]["n"], 1)
        self.assertNotIn("surface", miss)               # 都填了就不出现

    def test_summarize_missing_all_filled(self):
        rows = [{"code": "A", "material": "钢", "brand": "b", "surface": "发黑"}]
        self.assertEqual(bom.summarize_missing(rows), [])

    def test_summarize_missing_caps_sample(self):
        rows = [{"code": "", "name": "x%d" % i, "file": "f%d.sldprt" % i}
                for i in range(50)]
        m = bom.summarize_missing(rows)[0]
        self.assertEqual(m["n"], 50)                    # 数量报全
        self.assertEqual(len(m["files"]), 20)           # 明细只给前 20 个

    def test_export_includes_missing_attrs(self):
        with mock.patch.object(bom, "collect",
                               return_value=("总装", bom.parse_lines(SAMPLE)[1])):
            res = bom.export(self.asm, os.path.join(self.tmp, "B4"))
        self.assertIn("missing_attrs", res)
        self.assertIsInstance(res["missing_attrs"], list)

    def test_bad_snapshot_file_does_not_break_export(self):
        p = bom._snap_file(self.tmp, self.asm)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("{坏掉的 json")
        self.assertEqual(bom.load_snapshots(self.tmp, self.asm), [])
        with mock.patch.object(bom, "collect",
                               return_value=("总装", bom.parse_lines(SAMPLE)[1])):
            res = bom.export(self.asm, os.path.join(self.tmp, "B3"),
                             project_root=self.tmp)
        self.assertIsNone(res["diff"])
        self.assertTrue(os.path.isfile(res["xlsx"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
