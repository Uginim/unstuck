"""SQLite state: cases, the agent transcript (so a paused run can resume), timeline events, questions, actions."""
import json
import sqlite3
import time
import hashlib
from contextlib import contextmanager

# Public-demo cases store their scenario as "<scenario>~<workspace>"; local cases use the plain key.
# The workspace column is derived from it, so child cases (checks, created invoices) inherit their workspace.
WORKSPACE_SEP = "~"


def workspace_of(scenario):
    return scenario.partition(WORKSPACE_SEP)[2]


SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
  id TEXT PRIMARY KEY, scenario TEXT, invoice_id TEXT, client TEXT, title TEXT,
  state TEXT, messages TEXT, pending_results TEXT, final TEXT, usd REAL DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, at REAL, kind TEXT, label TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS questions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, tool_use_id TEXT, to_addr TEXT, question TEXT,
  hypothesis TEXT, evidence TEXT, state TEXT, reply TEXT, at REAL);
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, type TEXT, params TEXT, state TEXT, result TEXT, at REAL);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, case_id TEXT NOT NULL, workspace TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL, text TEXT NOT NULL, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS home_chat (
  id INTEGER PRIMARY KEY AUTOINCREMENT, workspace TEXT NOT NULL, user TEXT NOT NULL, reply TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS spend (day TEXT PRIMARY KEY, usd REAL);
"""


class Store:
    def __init__(self, path):
        self.path = path
        with self.db() as c:
            c.executescript(SCHEMA)
            columns = {r[1] for r in c.execute("PRAGMA table_info(cases)")}
            for column, definition in (("mode", "TEXT DEFAULT 'investigate'"), ("parent_id", "TEXT"),
                                       ("workspace", "TEXT NOT NULL DEFAULT ''")):
                if column not in columns:
                    c.execute(f"ALTER TABLE cases ADD COLUMN {column} {definition}")

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    # cases
    def add_case(self, case_id, scenario, invoice_id, client, title, mode="investigate", parent_id=None):
        with self.db() as c:
            c.execute("INSERT INTO cases (id,scenario,invoice_id,client,title,state,messages,pending_results,final,usd,created,mode,parent_id,workspace) "
                      "VALUES (?,?,?,?,?,?,?,?,?,0,?,?,?,?)",
                      (case_id, scenario, invoice_id, client, title, "new", "[]", "[]", None, time.time(), mode, parent_id,
                       workspace_of(scenario)))

    def case(self, case_id):
        with self.db() as c:
            r = c.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["messages"] = json.loads(d["messages"])
        d["pending_results"] = json.loads(d["pending_results"] or "[]")
        d["final"] = json.loads(d["final"]) if d["final"] else None
        return d

    def cases(self, workspace=None):
        """Investigation cases; pass a workspace ('' for local mode) to see only that visitor's cases."""
        q = "SELECT id, scenario, invoice_id, client, title, state, usd, mode, parent_id, workspace FROM cases WHERE mode='investigate'"
        args = ()
        if workspace is not None:
            q, args = q + " AND workspace=?", (workspace,)
        with self.db() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY created", args)]

    def unbilled_case(self, source, recipient, create=False):
        case_id = "ub-" + hashlib.sha256(recipient.casefold().encode()).hexdigest()[:20]
        case = self.case(case_id)
        if not case and create:
            self.add_case(case_id, source["scenario"], source["invoice_id"], source["client"],
                          "Unbilled work check", mode="unbilled", parent_id=source["id"])
            case = self.case(case_id)
        return case

    def scope_case(self, source, create=False):
        parent = source.get("parent_id") or source["id"]
        case_id = "sc-" + hashlib.sha256(parent.encode()).hexdigest()[:20]
        if create:
            # A single INSERT handles simultaneous clicks without replacing a paused run.
            with self.db() as c:
                c.execute("INSERT OR IGNORE INTO cases "
                          "(id,scenario,invoice_id,client,title,state,messages,pending_results,created,mode,parent_id,workspace) "
                          "VALUES (?,?,?,?,?,'new','[]','[]',?,'scope',?,?)",
                          (case_id, source["scenario"], source["invoice_id"], source["client"],
                           "New requests check", time.time(), parent, workspace_of(source["scenario"])))
        return self.case(case_id)

    def claim_case(self, case_id):
        with self.db() as c:
            return c.execute("UPDATE cases SET state='running' WHERE id=? AND state='new'", (case_id,)).rowcount == 1

    def claim_action(self, aid, state):
        with self.db() as c:
            return c.execute("UPDATE actions SET state=? WHERE id=? AND state='proposed'", (state, aid)).rowcount == 1

    def save_case(self, case_id, **fields):
        enc = {k: json.dumps(v, ensure_ascii=False) if k in ("messages", "pending_results", "final") else v for k, v in fields.items()}
        with self.db() as c:
            c.execute(f"UPDATE cases SET {', '.join(f'{k}=?' for k in enc)} WHERE id=?", (*enc.values(), case_id))

    def clear(self):
        with self.db() as c:
            for t in ("cases", "events", "questions", "actions", "documents", "home_chat"):
                c.execute(f"DELETE FROM {t}")
            for table in ("accounts", "workspaces"):
                if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                    c.execute(f"DELETE FROM {table}")

    def add_document(self, case_id, title, text):
        import uuid
        case = self.case(case_id)
        if not case:
            raise ValueError("Unknown case")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError("Title must be 1–200 characters")
        if not isinstance(text, str) or not text.strip() or len(text) > 20000 or '\x00' in text + title:
            raise ValueError("Document must be 1–20,000 characters of plain text")
        doc_id = "owner-" + uuid.uuid4().hex
        with self.db() as c:
            c.execute("INSERT INTO documents (id,case_id,workspace,title,text,created) VALUES (?,?,?,?,?,?)",
                      (doc_id, case_id, case.get("workspace") or "", title.strip(), text.strip(), time.time()))
        return doc_id

    def documents(self, case_id):
        case = self.case(case_id)
        if not case:
            return []
        with self.db() as c:
            rows = c.execute("SELECT id,title,text FROM documents WHERE case_id=? AND workspace=? ORDER BY created",
                             (case_id, case.get("workspace") or "")).fetchall()
        return [{**dict(r), "kind": "owner", "provenance": "Added by you"} for r in rows]

    # events
    def event(self, case_id, kind, label, detail=None):
        with self.db() as c:
            c.execute("INSERT INTO events (case_id, at, kind, label, detail) VALUES (?,?,?,?,?)",
                      (case_id, time.time(), kind, label, json.dumps(detail, ensure_ascii=False) if detail is not None else None))

    def events(self, case_id, after=0):
        with self.db() as c:
            rows = c.execute("SELECT * FROM events WHERE case_id=? AND id>? ORDER BY id", (case_id, after)).fetchall()
        return [{**dict(r), "detail": json.loads(r["detail"]) if r["detail"] else None} for r in rows]

    # questions
    def add_question(self, case_id, tool_use_id, to, question, hypothesis, evidence):
        with self.db() as c:
            cur = c.execute("INSERT INTO questions (case_id, tool_use_id, to_addr, question, hypothesis, evidence, state, at) "
                            "VALUES (?,?,?,?,?,?,?,?)", (case_id, tool_use_id, to, question, hypothesis,
                                                         json.dumps(evidence), "needs_approval", time.time()))
            return cur.lastrowid

    def question(self, qid):
        with self.db() as c:
            r = c.execute("SELECT * FROM questions WHERE id=?", (qid,)).fetchone()
        return dict(r) if r else None

    def questions(self, case_id=None, state=None):
        q, args = "SELECT * FROM questions WHERE 1=1", []
        if case_id:
            q, args = q + " AND case_id=?", args + [case_id]
        if state:
            q, args = q + " AND state=?", args + [state]
        with self.db() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY id", args)]

    def set_question(self, qid, **fields):
        with self.db() as c:
            c.execute(f"UPDATE questions SET {', '.join(f'{k}=?' for k in fields)} WHERE id=?", (*fields.values(), qid))

    # actions
    def add_action(self, case_id, type_, params):
        with self.db() as c:
            c.execute("INSERT INTO actions (case_id, type, params, state, at) VALUES (?,?,?,?,?)",
                      (case_id, type_, json.dumps(params, ensure_ascii=False), "proposed", time.time()))

    def actions(self, case_id):
        with self.db() as c:
            rows = c.execute("SELECT * FROM actions WHERE case_id=? ORDER BY id", (case_id,)).fetchall()
        return [{**dict(r), "params": json.loads(r["params"]), "result": json.loads(r["result"]) if r["result"] else None} for r in rows]

    def action(self, aid):
        with self.db() as c:
            r = c.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone()
        return {**dict(r), "params": json.loads(r["params"])} if r else None

    def set_action(self, aid, state, result=None):
        with self.db() as c:
            c.execute("UPDATE actions SET state=?, result=? WHERE id=?", (state, json.dumps(result, ensure_ascii=False, default=str), aid))

    def home_exchanges(self, workspace):
        with self.db() as c:
            rows = c.execute("SELECT user, reply FROM home_chat WHERE workspace=? ORDER BY id DESC LIMIT 6", (workspace,)).fetchall()
        return [{"user": r["user"], **json.loads(r["reply"])} for r in reversed(rows)]

    def add_home_exchange(self, workspace, user, reply):
        with self.db() as c:
            c.execute("INSERT INTO home_chat(workspace,user,reply) VALUES (?,?,?)",
                      (workspace, user, json.dumps(reply, ensure_ascii=False)))

    # spend
    def add_spend(self, case_id, usd):
        day = time.strftime("%Y-%m-%d")
        with self.db() as c:
            c.execute("INSERT INTO spend VALUES (?,?) ON CONFLICT(day) DO UPDATE SET usd=usd+excluded.usd", (day, usd))
            c.execute("UPDATE cases SET usd=usd+? WHERE id=?", (usd, case_id))

    def spent_today(self):
        with self.db() as c:
            r = c.execute("SELECT usd FROM spend WHERE day=?", (time.strftime("%Y-%m-%d"),)).fetchone()
        return r["usd"] if r else 0.0
