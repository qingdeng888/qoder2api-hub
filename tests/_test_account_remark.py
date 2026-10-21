"""账号备注（纯本地字段）套件。

    python tests/_test_account_remark.py

覆盖：Account 读写/截断、public() 暴露、AccountPool.set_remark 落盘、
导入导出往返保留、面板接线。
"""
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(ROOT, "_acc"))
os.environ.setdefault("QD_NATIVE_IDENTITY", "0")

import qoder_accounts as A

PASS = FAIL = 0


def check(label, ok, detail=None):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s %r" % (label, detail))


def main():
    print("[account-remark] 账号备注")
    temp = tempfile.mkdtemp(prefix="qd-remark-")
    try:
        account = A.Account({
            "uid": "remark-1", "nickname": "主号", "realm": "cn",
            "accessToken": "dt-x", "remark": "  工作号 A  ",
        })
        check("Account 读取备注并去空白", account.remark == "工作号 A",
              account.remark)
        check("public() 暴露 remark", account.public().get("remark") == "工作号 A",
              account.public().get("remark"))
        check("to_dict() 持久化 remark",
              account.to_dict().get("remark") == "工作号 A")

        long_text = "x" * 300
        truncated = A.Account({"uid": "remark-2", "accessToken": "dt-x",
                               "remark": long_text})
        check("备注超长被截断到 120 字", len(truncated.remark) == 120,
              len(truncated.remark))

        legacy = A.Account({"uid": "remark-3", "accessToken": "dt-x"})
        check("旧账号文件没有备注 -> 空字符串", legacy.remark == "",
              legacy.remark)
        alias = A.Account({"uid": "remark-4", "accessToken": "dt-x",
                           "note": "别名写法"})
        check("兼容 note 字段", alias.remark == "别名写法", alias.remark)

        pool = A.AccountPool(temp)
        pool.add(A.Account({"uid": "remark-10", "nickname": "A",
                            "realm": "cn", "accessToken": "dt-a"}))
        pool.add(A.Account({"uid": "remark-11", "nickname": "B",
                            "realm": "cn", "accessToken": "dt-b"}))
        updated = pool.set_remark("remark-10", "家庭宽带 · 小号")
        check("set_remark 返回更新后的 public",
              updated.get("remark") == "家庭宽带 · 小号", updated.get("remark"))
        check("另一个账号不受影响",
              pool.get("remark-11").remark == "", pool.get("remark-11").remark)

        reloaded = A.AccountPool(temp)
        reloaded.load()
        check("重新加载后备注仍在",
              reloaded.get("remark-10").remark == "家庭宽带 · 小号",
              reloaded.get("remark-10").remark)

        path = reloaded.get("remark-10").path
        with open(path, encoding="utf-8") as fh:
            disk = json.load(fh)
        check("备注写进账号 JSON", disk.get("remark") == "家庭宽带 · 小号",
              disk.get("remark"))

        pool.set_remark("remark-10", "")
        check("传空字符串 = 清除备注",
              pool.get("remark-10").remark == "", pool.get("remark-10").remark)

        try:
            pool.set_remark("does-not-exist", "x")
            raised = False
        except KeyError:
            raised = True
        check("未知 uid 抛 KeyError", raised)

        pool.set_remark("remark-11", "导出测试")
        doc = A.build_export_document(pool.accounts, include_secrets=True)
        row = [item for item in doc["accounts"]
               if item.get("uid") == "remark-11"][0]
        check("导出文档含 remark", row.get("remark") == "导出测试",
              row.get("remark"))

        rows, problem = A._coerce_account_rows(doc)
        check("导出文档可再次解析", not problem and len(rows) == 2,
              (problem, len(rows)))
        kwargs = A.normalise_import_row(rows[1])
        # 上游 #69 把备注统一叫 note；本 fork 的 remark 是它的别名
        check("导入保留备注（note）", kwargs.get("note") == "导出测试",
              kwargs.get("note"))
        check("导入保留出口绑定字段",
              "proxyMode" in kwargs and "proxyId" in kwargs, sorted(kwargs))
    finally:
        shutil.rmtree(temp, ignore_errors=True)

    html = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
    # 面板备注 UI 统一走上游 #69 的实现（/accounts/note + editAccountNote）
    for token in ("editAccountNote", "/accounts/note", "备注"):
        check("面板接线: %s" % token, token in html)
    src = open(os.path.join(ROOT, "qoder_proxy.py"), encoding="utf-8").read()
    check("备注接口同时保留 fork 的 /accounts/remark 别名",
          '"/accounts/note"' in src and '"/accounts/remark"' in src)

    print("")
    print("SUMMARY: TOTAL %d checks, %d passed, %d failed"
          % (PASS + FAIL, PASS, FAIL))
    print("RESULT: %s (exit %d)" % ("RED" if FAIL else "GREEN",
                                    1 if FAIL else 0))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
