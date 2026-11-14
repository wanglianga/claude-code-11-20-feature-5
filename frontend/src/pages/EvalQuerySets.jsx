import { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'

const TYPE_LABEL = { concept: '常见概念', required_chapter: '必考章节', example: '例题题型' }
const GRADE_LABEL = { 1: '1 级·弱相关', 2: '2 级·相关', 3: '3 级·高度相关' }

export default function EvalQuerySets() {
  const { user } = useAuth()
  const isEditor = user?.role === 'editor'
  const [sets, setSets] = useState([])
  const [meta, setMeta] = useState({ stages: [], subjects: [] })
  const [detail, setDetail] = useState(null)
  const [msg, setMsg] = useState('')
  const [form, setForm] = useState({
    name: '', description: '', stages: [], subjects: ['数学'],
  })
  const [qForm, setQForm] = useState({ query_text: '', question_type: 'concept', volume: '', note: '' })
  const [samples, setSamples] = useState([])

  async function load() {
    setSets(await api.get('/api/eval/query-sets'))
    setSamples(await api.get('/api/eval/disabled-samples?status=all'))
  }
  useEffect(() => {
    api.get('/api/meta').then(setMeta).catch(() => {})
    load()
  }, [])

  async function openSet(id) {
    setMsg('')
    setDetail(await api.get(`/api/eval/query-sets/${id}`))
  }

  function toggle(arrKey, val) {
    setForm({ ...form, [arrKey]: form[arrKey].includes(val)
      ? form[arrKey].filter((x) => x !== val)
      : [...form[arrKey], val] })
  }

  async function createSet(e) {
    e.preventDefault()
    setMsg('')
    try {
      const s = await api.post('/api/eval/query-sets', {
        name: form.name, description: form.description,
        scope: { stages: form.stages, subjects: form.subjects, isbns: [], include_deprecated: false },
      })
      setForm({ name: '', description: '', stages: [], subjects: ['数学'] })
      await load()
      await openSet(s.set_id)
      setMsg('查询集已建立（草稿态，可继续添加查询与标注）')
    } catch (err) { setMsg(err.message) }
  }

  async function addQuery(e) {
    e.preventDefault()
    if (!detail || detail.status !== 'draft') return
    try {
      await api.post(`/api/eval/query-sets/${detail.set_id}/queries`, qForm)
      setQForm({ query_text: '', question_type: 'concept', volume: '', note: '' })
      await openSet(detail.set_id)
    } catch (err) { setMsg(err.message) }
  }

  async function freeze() {
    if (!window.confirm('冻结后索引、标注与配置将被固定，不能再修改，确认冻结？')) return
    try {
      const r = await api.post(`/api/eval/query-sets/${detail.set_id}/freeze`)
      setMsg(`已冻结：${r.status}`)
      await openSet(detail.set_id)
      await load()
    } catch (err) { setMsg(`冻结被拒绝：${err.message}`) }
  }

  async function disableQuery(qid) {
    const reason = window.prompt('停用原因（该查询不再参与评测）：', '维护停用')
    if (reason === null) return
    await api.post(`/api/eval/queries/${qid}/disable`, { reason })
    await openSet(detail.set_id)
  }

  return (
    <div className="eval-page">
      <h2>评测查询集（教师建立 · 教研员裁定 · 冻结后固定）</h2>
      {msg && <div className="notice">{msg}</div>}

      <section className="card">
        <h3>第一步：建立带适用范围的查询集</h3>
        <form onSubmit={createSet}>
          <div className="filter-row">
            <input placeholder="查询集名称，如 初中数学教师评测集" value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })} required />
            <input placeholder="说明（可选）" value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })} />
          </div>
          <div className="filter-row">
            <span className="filter-label">学段</span>
            {meta.stages.map((s) => (
              <button type="button" key={s} className={`chip ${form.stages.includes(s) ? 'on' : ''}`}
                onClick={() => toggle('stages', s)}>{s}</button>
            ))}
          </div>
          <div className="filter-row">
            <span className="filter-label">学科</span>
            {meta.subjects.map((s) => (
              <button type="button" key={s} className={`chip ${form.subjects.includes(s) ? 'on' : ''}`}
                onClick={() => toggle('subjects', s)}>{s}</button>
            ))}
          </div>
          <button className="btn primary" type="submit">建立查询集（草稿）</button>
        </form>
      </section>

      <section className="card">
        <h3>查询集列表</h3>
        <table className="grid">
          <thead><tr><th>ID</th><th>名称</th><th>适用范围</th><th>状态</th><th>查询数</th><th>操作</th></tr></thead>
          <tbody>
            {sets.map((s) => (
              <tr key={s.set_id}>
                <td>{s.set_id}</td>
                <td>{s.name}<div className="muted small">{s.description}</div></td>
                <td className="small">{JSON.stringify(s.scope)}</td>
                <td><span className={`badge syn-${s.status === 'frozen' ? 'approved' : 'pending'}`}>{s.status}</span></td>
                <td>{s.query_count}</td>
                <td><button className="btn ghost" onClick={() => openSet(s.set_id)}>打开</button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {detail && (
        <section className="card">
          <div className="ev-head">
            <h3>查询集 {detail.set_id}：{detail.name}
              <span className={`badge syn-${detail.status === 'frozen' ? 'approved' : 'pending'}`} style={{ marginLeft: 10 }}>
                {detail.status === 'frozen' ? '已冻结（输入固定）' : '草稿（可编辑）'}
              </span>
            </h3>
            {detail.status === 'draft' &&
              <button className="btn primary" onClick={freeze}>冻结查询集</button>}
          </div>

          {detail.status === 'draft' && (
            <form className="syn-form" onSubmit={addQuery}>
              <input placeholder="查询文本，如 定义域" value={qForm.query_text}
                onChange={(e) => setQForm({ ...qForm, query_text: e.target.value })} required />
              <select value={qForm.question_type}
                onChange={(e) => setQForm({ ...qForm, question_type: e.target.value })}>
                {Object.entries(TYPE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </select>
              <input placeholder="册次，如 八年级下册" value={qForm.volume}
                onChange={(e) => setQForm({ ...qForm, volume: e.target.value })} />
              <button className="btn primary" type="submit">添加查询</button>
            </form>
          )}

          <table className="grid" style={{ marginTop: 12 }}>
            <thead><tr><th>查询</th><th>题型/册次</th><th>状态</th><th>标注（等级 · 必需出处 · 等价集合）</th></tr></thead>
            <tbody>
              {detail.queries.map((q) => (
                <QueryRow key={q.query_id} q={q} frozen={detail.status === 'frozen'}
                  isEditor={isEditor} scope={detail.scope}
                  onChanged={() => openSet(detail.set_id)} setId={detail.set_id}
                  onDisable={() => disableQuery(q.query_id)} />
              ))}
            </tbody>
          </table>
        </section>
      )}

      <DisabledSamples samples={samples} isEditor={isEditor} onChanged={load} />
    </div>
  )
}

function QueryRow({ q, frozen, isEditor, scope, onChanged, setId, onDisable }) {
  const [annForm, setAnnForm] = useState({ unit_code: '', grade: 2, required_source: false, equiv_group: '', note: '' })
  const [picks, setPicks] = useState(null)
  const [err, setErr] = useState('')

  async function addAnn(e) {
    e.preventDefault()
    setErr('')
    try {
      await api.post(`/api/eval/queries/${q.query_id}/annotations`, {
        ...annForm, equiv_group: annForm.equiv_group || null,
      })
      setAnnForm({ unit_code: '', grade: 2, required_source: false, equiv_group: '', note: '' })
      onChanged()
    } catch (e2) { setErr(e2.message) }
  }

  async function pickFromSearch() {
    const r = await api.post('/api/search', {
      query: q.query_text, stages: scope.stages || [], subjects: scope.subjects || [],
      isbns: scope.isbns || [], include_deprecated: scope.include_deprecated || false,
      page: 1, page_size: 10,
    })
    setPicks(r.hits)
  }

  async function adjudicate(keep) {
    const annotations = q.annotations
      .filter((a) => a.status !== 'rejected' && keep.includes(a.annotation_id))
      .map((a) => ({ unit_code: a.unit_code, grade: a.grade,
        required_source: a.required_source, equiv_group: a.equiv_group, note: a.note }))
    if (annotations.length === 0) { setErr('裁定至少保留一条标注'); return }
    try {
      await api.post(`/api/eval/query-sets/${setId}/adjudicate`, { query_id: q.query_id, annotations })
      onChanged()
    } catch (e2) { setErr(e2.message) }
  }

  const hasConflict = new Set(q.annotations.filter((a) => a.status !== 'rejected')
    .map((a) => `${a.unit_code}:${a.grade}`)).size !==
    new Set(q.annotations.filter((a) => a.status !== 'rejected').map((a) => a.unit_code)).size

  return (
    <tr>
      <td><strong>{q.query_text}</strong>
        {q.status === 'disabled' && <div className="badge gen-retired">已停用：{q.disabled_reason}</div>}
        {err && <div className="error small">{err}</div>}
      </td>
      <td>{TYPE_LABEL[q.question_type]}<div className="muted small">{q.volume || '未分册'}</div></td>
      <td>{hasConflict && !frozen ? <span className="badge bad">待仲裁</span> :
        <span className="badge ok">{q.annotations.some((a) => a.status === 'adjudicated') ? '已裁定' : '教师标注'}</span>}</td>
      <td>
        {q.annotations.length === 0 && <span className="muted small">尚无标注，冻结前必须补齐</span>}
        {q.annotations.map((a) => (
          <div key={a.annotation_id} className={`ann-row ${a.status === 'rejected' ? 'ann-rejected' : ''}`}>
            <code>{a.unit_code}</code>
            <span className="badge syn-pending">{GRADE_LABEL[a.grade]}</span>
            {a.required_source && <span className="badge req">必需出处</span>}
            {a.equiv_group && <span className="badge equiv">等价集合 {a.equiv_group.slice(0, 8)}</span>}
            <span className="muted small">{a.annotator} · {a.status}</span>
            {!frozen && isEditor && a.status !== 'rejected' && (
              <button className="btn ghost danger small-btn"
                onClick={() => adjudicate(q.annotations
                  .filter((x) => x.annotation_id !== a.annotation_id)
                  .map((x) => x.annotation_id))}>驳回此条</button>
            )}
          </div>
        ))}
        {!frozen && q.status === 'active' && (
          <form className="ann-form" onSubmit={addAnn}>
            <input placeholder="段落标识 unit_code" value={annForm.unit_code}
              onChange={(e) => setAnnForm({ ...annForm, unit_code: e.target.value })} required />
            <select value={annForm.grade}
              onChange={(e) => setAnnForm({ ...annForm, grade: Number(e.target.value) })}>
              {Object.entries(GRADE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
            <label className="switch"><input type="checkbox" checked={annForm.required_source}
              onChange={(e) => setAnnForm({ ...annForm, required_source: e.target.checked })} />必需出处</label>
            <input placeholder="等价集合 UUID（可空，多个等价答案填同一值）"
              value={annForm.equiv_group}
              onChange={(e) => setAnnForm({ ...annForm, equiv_group: e.target.value })} />
            <button className="btn ghost" type="button" onClick={pickFromSearch}>从检索结果选择</button>
            <button className="btn primary" type="submit">添加标注</button>
            <button className="btn ghost danger" type="button" onClick={onDisable}>停用查询</button>
          </form>
        )}
        {!frozen && isEditor && q.status === 'active' && (
          <button className="btn ghost" style={{ marginTop: 6 }}
            onClick={() => adjudicate(q.annotations.filter((a) => a.status !== 'rejected')
              .map((a) => a.annotation_id))}>
            教研员裁定（保留当前有效标注，驳回其余意见）
          </button>
        )}
        {picks && (
          <div className="pick-box">
            <div className="muted small">点击命中条目填充段落标识（只认可选集合中的真实段落）：</div>
            {picks.map((h) => (
              <button key={h.unit_id} type="button" className="chip wide"
                onClick={() => { setAnnForm({ ...annForm, unit_code: h.unit_code }); setPicks(null) }}>
                {h.unit_code} ｜ {h.title_path} ｜ {h.text.slice(0, 30)}…
              </button>
            ))}
            {picks.length === 0 && <span className="muted small">该范围下无命中，请手输段落标识</span>}
          </div>
        )}
      </td>
    </tr>
  )
}

function DisabledSamples({ samples, isEditor, onChanged }) {
  const [form, setForm] = useState({ generation_id: '', unit_code: '', reason: '' })
  async function submit(e) {
    e.preventDefault()
    try {
      await api.post('/api/eval/disabled-samples',
        { ...form, generation_id: Number(form.generation_id) })
      setForm({ generation_id: '', unit_code: '', reason: '' })
      onChanged()
    } catch (err) { window.alert(err.message) }
  }
  async function decide(id, status) {
    await api.post(`/api/eval/disabled-samples/${id}/decide`, { status })
    onChanged()
  }
  return (
    <section className="card">
      <h3>段落停用标记样本（待维护；裁定应用后才在后续评测中排除）</h3>
      <form className="syn-form" onSubmit={submit}>
        <input placeholder="索引代次 generation_id" value={form.generation_id}
          onChange={(e) => setForm({ ...form, generation_id: e.target.value })} required />
        <input placeholder="段落标识 unit_code" value={form.unit_code}
          onChange={(e) => setForm({ ...form, unit_code: e.target.value })} required />
        <input placeholder="停用原因" value={form.reason}
          onChange={(e) => setForm({ ...form, reason: e.target.value })} required />
        <button className="btn primary" type="submit">登记待维护样本</button>
      </form>
      <table className="grid" style={{ marginTop: 10 }}>
        <thead><tr><th>代次</th><th>教材</th><th>段落</th><th>原因</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
          {samples.map((d) => (
            <tr key={d.sample_id}>
              <td>{d.generation_id}</td><td>{d.textbook_title}</td><td><code>{d.unit_code}</code></td>
              <td>{d.reason}</td>
              <td><span className={`badge syn-${d.status === 'applied' ? 'approved' : d.status === 'ignored' ? 'rejected' : 'pending'}`}>{d.status}</span></td>
              <td>{d.status === 'pending' && isEditor && <>
                <button className="btn ghost" onClick={() => decide(d.sample_id, 'applied')}>应用排除</button>
                <button className="btn ghost danger" onClick={() => decide(d.sample_id, 'ignored')}>忽略</button>
              </>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}
