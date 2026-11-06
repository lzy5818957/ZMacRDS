import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location("cross",ROOT/"tools/m1-cross-verify.py")
CROSS=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(CROSS)


def fixture():
    agents=[];editors=[]
    for uid in (502,503):
        identity=dict(uid=uid,gui_uid=uid,login_complete=True,audit_session_id=uid+1000,boot_id="boot")
        first=dict(identity,pid=uid+2000)
        editor_first=dict(identity,kind="start",pid=uid+3000,nonce=uid+4000,case_id="case",uptime_ms=0,input_source="com.apple.keylayout.ABC",console_uid=502)
        erows=[editor_first];rows=[];hbs=[]
        for j,console in enumerate((502,503)):
            t=10000+j*10000
            rows.append(dict(kind="script_started",uptime_ms=t,console_uid=console,editor_pid=editor_first["pid"],editor_nonce=editor_first["nonce"]))
            hbs.append(dict(uptime_ms=t+500,console_uid=console,accessibility_trusted=True,connected=True))
            tag=f"{uid:08x}{j:08x}"
            rows.append(dict(kind="script_post",tag=tag,event_type=10,uptime_ms=t+100,route="kCGSessionEventTap"))
            erows.append(dict(editor_first,kind="event",tag=tag,event_type=10,uptime_ms=t+110,console_uid=console))
            for label,text in [("unicode-1",f"用户{uid}：中文001🌍"),("pinyin-1","你好世界"),("agent-10","你好世界")]:
                document=dict(editor_first,kind="document",text=text,marked=False,selection_length=4,uptime_ms=t+200,console_uid=console)
                erows.append(document)
                rows.append(dict(kind="checkpoint",label=label,ok=True,editor=document,uptime_ms=t+210,console_uid=console,frontmost_pid=first["pid"] if label.startswith("agent") else editor_first["pid"]))
            erows.append(dict(editor_first,kind="composition",text="nihao",input_source="com.apple.inputmethod.SCIM.ITABC",console_uid=console,uptime_ms=t+150))
            rows.append(dict(kind="script_finished",uptime_ms=t+1000))
        agents.append(SimpleNamespace(uid=uid,first=first,rows=rows,heartbeats=hbs));editors.append(erows)
    return agents,editors


class CrossTests(unittest.TestCase):
    def test_cross_process_documents_and_real_composition(self):
        CROSS.verify_cross(*fixture(),rounds=1)

    def test_activation_click_requires_timely_focus_evidence(self):
        agents,editors=fixture();a=agents[0]
        a.rows.insert(1,dict(kind="script_action",action=dict(type="focus",target="agent")))
        a.rows[2]["event_type"]=1
        editors[0]=[r for r in editors[0] if not (r["kind"]=="event" and r["tag"]==a.rows[2]["tag"])]
        CROSS.verify_cross(agents,editors,rounds=1)
        next(r for r in a.rows if r["kind"]=="checkpoint" and r["label"]=="agent-10")["uptime_ms"]+=3000
        with self.assertRaises(CROSS.CAPTURE.Problem):CROSS.verify_cross(agents,editors,rounds=1)

    def test_rejects_false_positive_cross_app_results(self):
        for fault in ("same-process","wrong-session","foreign-event","missing-receipt","text","selection","no-ime","console-change","abort"):
            agents,editors=fixture();a=agents[0]
            if fault=="same-process":editors[0][0]["pid"]=a.first["pid"]
            elif fault=="wrong-session":editors[0][0]["audit_session_id"]+=1
            elif fault=="foreign-event":editors[1].append(dict(editors[1][0],kind="event",tag=next(r for r in a.rows if r["kind"]=="script_post")["tag"]))
            elif fault=="missing-receipt":editors[0]=[r for r in editors[0] if r["kind"]!="event"]
            elif fault in ("text","selection"):
                row=next(r for r in a.rows if r["kind"]=="checkpoint" and r["label"]=="unicode-1")
                row["editor"]=dict(row["editor"])
                row["editor"]["text" if fault=="text" else "selection_length"]="wrong" if fault=="text" else 0
            elif fault=="no-ime":editors[0]=[r for r in editors[0] if r["kind"]!="composition"]
            elif fault=="console-change":a.heartbeats[0]["console_uid"]=503
            elif fault=="abort":a.rows.append(dict(kind="script_aborted"))
            with self.subTest(fault=fault),self.assertRaises(CROSS.CAPTURE.Problem):
                CROSS.verify_cross(agents,editors,rounds=1)

    def test_recovery_requires_no_disconnected_output_and_same_document(self):
        def recovery():
            a=SimpleNamespace(rows=[
                dict(kind="disconnected",reason="requested",uptime_ms=10000,epoch=2),
                dict(kind="script_rejected",reason="stale identity or epoch",uptime_ms=11000),
                dict(kind="reconnected",uptime_ms=40000,epoch=3),
                dict(kind="checkpoint",label="retained-document",uptime_ms=42000,ok=True,editor=dict(text="你好世界",pid=20)),
                dict(kind="checkpoint",label="resume-edited",uptime_ms=43000,ok=True)])
            b=SimpleNamespace(verify_interval=lambda start,end: None,rows=[dict(kind="script_started",uptime_ms=12000),dict(kind="script_finished",uptime_ms=30000)])
            editor=[dict(kind="document",uptime_ms=9000,text="你好世界",pid=20)]
            return a,b,editor
        CROSS.verify_recovery(*recovery())
        for fault in ("output","epoch","document","pid","no-rejection","other-input"):
            a,b,e=recovery()
            if fault=="output":a.rows.append(dict(kind="script_post",uptime_ms=25000))
            elif fault=="epoch":a.rows[2]["epoch"]=2
            elif fault=="document":a.rows[3]["editor"]["text"]=""
            elif fault=="pid":a.rows[3]["editor"]["pid"]=21
            elif fault=="no-rejection":a.rows.pop(1)
            elif fault=="other-input":b.rows=[]
            with self.subTest(fault=fault),self.assertRaises(CROSS.CAPTURE.Problem):CROSS.verify_recovery(a,b,e)


if __name__=="__main__":unittest.main()
