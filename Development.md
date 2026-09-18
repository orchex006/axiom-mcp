# Development — axiom-mcp

## Repository identity

| Field | Value |
| --- | --- |
| Component | `axiom-mcp` |
| Canonical remote | `https://github.com/orchex006/axiom-mcp` |
| Owns | MCP server surface ที่ expose Axiom graph operations ให้ host/agent |
| Does not own | graph runtime (`axiom-graphd`), ecosystem contracts (`axiom-specs`), skills (`axiom-skills`) |

Take only tasks whose `repo` is `axiom-mcp`. Public commands, schema/layout versions, tool contracts และ compatibility decisions ต้อง coordinat ผ่าน `axiom-specs` ก่อน; repository นี้ implement contract ไม่ใช่กำหนด contract เอง

## Pinned governance

Canonical workflow คือ `Development.md` ใน `axiom-specs` ณ revision ที่ `spec.lock.json` pin ไว้

สถานะปัจจุบัน: repository นี้ยังไม่มี implementation แต่มี `AGENTS.md` และ `spec.lock.json` ที่ verify แล้ว (immutable revision และ contract digests ผ่าน `tools/spec-lock-check.py` ด้วย exit code 0) งาน implementation จึงไม่ถูก block ด้วย governance pin อีก งานชิ้นแรกที่เข้า repository นี้ MUST สร้าง required check `python -m pytest tests -q` และ MUST บันทึก check ที่ยังไม่ได้รันเป็น unverified

## Preflight — ก่อนเริ่มทุก task

1. อ่าน workspace `AGENTS.md`, `Development.md` ของ repository นี้ และ task card/spec ที่เกี่ยวข้อง;
2. ตรวจ repository identity (`git remote -v`) ว่าตรงกับ canonical remote;
3. ตรวจ `git status` และ preserve งานที่ยังไม่ commit ของเจ้าของ;
4. ระบุ approved base branch (`main`) และ synchronize อย่างปลอดภัยเมื่อ state อนุญาต;
5. สร้างหรือเข้า `feature/<task-id>-<slug>`;
6. ระบุ allowed files, required tests และ explicit commit/push authority;
7. บันทึก base revision, dirty state และคำสั่งที่จะใช้เป็น evidence.

ห้ามเริ่มแก้ task-owned files ก่อนครบทั้ง 7 ข้อ

## During work

- ทำหนึ่ง bounded task ต่อหนึ่ง branch;
- tool contract, error envelope และ exit code ต้องสอดคล้องกับ canonical contract ใน `axiom-specs` และ runtime จริง;
- ต้องมี positive, negative และ failure-boundary test สำหรับ behavior ที่รับมาจาก graph runtime;
- ห้าม hard-code assumption ที่ผูกกับเครื่องผู้พัฒนา; behavior ต้องไม่พึ่ง Bash, WSL, Docker, administrator privileges หรือ symlink;
- local implementation documentation อยู่ใน repository นี้ และต้องถูกแก้พร้อม implementation

## Required checks

C-001 established this repository's required checks (stack: Python 3.13 + official MCP SDK `mcp==1.28.1` + FastAPI):

```text
python -m pytest tests -q
python -m ruff check .
python -m ruff format --check .
```

All later tasks MUST run these against the final bytes and record the real command and exit code in
evidence. A check that did not run MUST be recorded as unverified, never reported as passing.

## Evidence และ completion

- ใช้ canonical six preflight check และ eight completion check;
- attach จริง: test output, exit code, hash ของ artifact, acceptance mapping, changed-file review, compatibility/rollback note;
- target ที่ยังไม่ได้รัน MUST ถูกบันทึกเป็น unverified ไม่ใช่ปล่อยว่าง;
- ห้ามอ้างว่า "น่าจะผ่าน" แทนการรันจริง

## Branch/release policy

Canonical policy: `Development.md` §3 (`Branch/worktree policy`) ใน `axiom-specs` ณ revision ที่ pin ไว้

| Branch | ใช้สำหรับ | การรวม |
| --- | --- | --- |
| `main` | integration branch ของงานพัฒนา | merge จาก task branch ที่ผ่าน merge gate; ห้าม push implementation commit ตรงเข้า `main` |
| `feature/<task-id>-<slug>` | task branch ต่อหนึ่ง bounded task | merge เข้า `main` เมื่อผ่าน merge gate; PR เมื่อ repo ต้องมี reviewer |
| `release/vX.Y.Z` | release candidate, stabilization และจุดออก release | เฉพาะ release authority; ห้าม merge เข้า release branch เอง |

ไม่สร้างหรือใช้ `develop` ใน repository นี้

### Merge gate

Agent MAY merge task branch เข้า `main` ได้เองโดยไม่ต้องขอ authorization เพิ่มเติม เมื่อครบทุกข้อ:

1. task status เป็น `done` และ ledger ตรงกับ card;
2. acceptance ครบทุกข้อ พร้อม command evidence จริง;
3. required checks ของ repository ผ่านจริง และบันทึกคำสั่งกับ exit code จริง;
4. final diff ถูก review แล้วว่าไม่หลุด scope และไม่รวม human-owned/unrelated changes;
5. `Changelog.md` และ docs ที่ได้รับผลถูกอัปเดตแล้ว;
6. ไม่มี blocker หรือ limitation ที่ยังไม่ถูกบันทึกใน evidence;
7. merge ไม่ทับงานที่ยังไม่ commit ของ checkout ที่เกี่ยวข้อง.

Agent MUST NOT merge เมื่อ task ยัง `in_progress`/`blocked`, evidence ไม่ครบ, required checks ไม่ผ่าน/ยังไม่ได้รัน หรือ merge ก่อ conflict ที่ต้องตัดสินใจเชิง requirement/contract

หลัง merge MUST push `main` ขึ้น canonical remote ตรวจ remote SHA และรายงาน merge commit SHA กับ main remote SHA

Merge เข้า `release/vX.Y.Z`, การ tag และ publish MUST มี release authorization ที่ระบุชัดเจนเสมอ

### Worktree และ checkout หลัก

งานที่ทำใน worktree MUST NOT จบอยู่แค่ใน worktree เมื่อ task verified แล้ว MUST:

1. integrate เข้า `main` ตาม merge gate ข้างต้น; และ
2. อัปเดต checkout หลัก (`D:\SP-Billy\axiom\axiom-mcp`) ให้ตรงกับ branch ที่ integrate แล้ว เมื่อ working tree ของ checkout นั้นสะอาดพอ; หรือ
3. ถ้าทำไม่ได้เพราะมีงานที่ยังไม่ commit ของเจ้าของ checkout ให้ preserve งานนั้นไว้ และรายงานชัดเจนว่า checkout หลักยังไม่ได้รับงาน พร้อมขั้นตอนถัดไปที่เฉพาะเจาะจง

ห้ามรายงานว่างาน "เสร็จ" โดยไม่ระบุว่า checkout หลักได้งานแล้วหรือยัง

รายงานปิดงาน MUST ระบุ: task ID, branch, commit SHA, สถานะการ integrate เข้า `main` และ path ของ checkout หลักที่อัปเดตแล้ว

## Commit และ push

- stage เฉพาะไฟล์ของ task; ห้าม `git add .` เมื่อมีงานอื่นใน working tree;
- conventional commit style เช่น `feat(mcp): ...`, `fix(mcp): ...`, `docs(mcp): ...`;
- commit แล้ว push feature branch ขึ้น canonical remote และรายงาน branch name กับ commit SHA;
- ห้าม force-push, ห้าม amend commit ของผู้อื่น, ห้าม rewrite published history;
- commit/push ล้มเหลว MUST รายงาน blocker ที่แท้จริงและเก็บงานที่ verify แล้วไว้ในเครื่อง

## Version และ release

- `axiom-mcp` version แยกจาก `axiom-graphd` และ `axiom-skills`;
- MCP tool schema/contract version ต้อง coordinat กับ `axiom-specs`; ห้ามเปลี่ยน public contract เองโดยไม่ผ่าน spec;
- tag, release branch และ publish ต้องมี release authorization เสมอ

## Definition of blocked

รายงานเป็น blocked เมื่อ: canonical pin หายหรือ verify ไม่ได้; required check รันไม่ได้; มี conflict ที่ต้องตัดสินใจเชิง contract; checkout หลัก/worktree มีงานที่ยังไม่ commit ซึ่งทำให้ integrate ไม่ปลอดภัย; หรือ task ต้องใช้ authorization เกิน standing workflow

## Cross-repository

เมื่อ task นี้กระทบ `axiom-specs`, `axiom-graphd` หรือ `axiom-skills` ต้องแยก child task ต่อ repository และแต่ละ repository ใช้ branch/verification/commit/push lifecycle ของตัวเอง
