import { useEffect, useState } from 'react'
import { api } from '../api.js'

const PACKAGES = [
  { isbn: '9787100000011', rel: '9787100000011/payload.json', label: '初中数学·旧版（第1代）' },
  { isbn: '9787100000011', rel: '9787100000011-gen2/payload.json', label: '初中数学·新版（第2代，删旧例题）' },
  { isbn: '9787100000028', rel: '9787100000028/payload.json', label: '高中数学·必修一（同名“函数”）' },
  { isbn: '9787100000035', rel: '9787100000035/payload.json', label: '初中物理（含缺失坏章）' },
]

export default function EditorBuild() {
  const [gens, setGens] = useState([])
  const [issues, setIssues] = useState([])
  const [report, setReport] = useState(null)
  const [msg, setMsg] = useState('')
  const [selected, setSelected] = useState(PACKAGES[3])
  const [partial, setPartial] = useState(true)

  async function load() {
    setGens(await api.get('/api/admin/generations'))
    setIssues(await api.get('/api/admin/issues?status=open'))
  }
  useEffect(() => { load() }, [])

  async function runBuild() {
    setMsg('')
    try {
      const body = {
        isbn: selected.isbn,
        payload_rel: selected.rel,
        chapter_codes: partial && selected.isbn === '9787100000035' ? ['W-CH13'] : null,
      }
      const r = await api.post('/api/admin/build', body)
      setReport(r)
      setMsg(`构建完成：状态 ${r.status}，成功 ${r.ok_chapters}/${r.total_chapters} 章`)
    } catch (e) {
      setMsg(`构建被拒绝：${e.message}`)
    }
    await load()
  }

  async function showReport(gid) {
    setReport(await api.get(`/api/admin/reports/${gid}`))
  }

  async function resolveIssue(id) {
    await api.post(`/api/admin/issues/${id}/resolve`)
    await load()
  }

  async function submitCorrection(issue) {
    const note = window.prompt(
      `提交章节 ${issue.chapter_code} 的修正稿说明（将存档并关闭该待修正项；重新入索引需用新一代次数据包重建）：`,
      '已补齐章节结构与正文，等待重新构建')
    if (note === null) return
    const gens = await api.get('/api/admin/generations')
    const target = gens.find((g) => g.generation_id === issue.generation_id)
    const r = await api.post('/api/admin/corrections', {
      edition_id: target.edition_id,
      chapter_code: issue.chapter_code,
      payload: { fix_note: note, fixed_by: 'editor1' },
    })
    setMsg(`修正稿已存档，关闭问题 ${r.resolved_issue_ids.join(',')}；失败代次仍需重新构建才能启用。`)
    await load()
  }

  async function retire(gid) {
    setMsg('')
    try {
      await api.post(`/api/admin/generations/${gid}/retire`)
      setMsg(`代次 ${gid} 已退役（retired），引用它的旧引用复核时将诚实失败`)
    } catch (e) {
      setMsg(e.message)
    }
    await load()
  }

  return (
    <div className="editor-page">
      <h2>索引构建监控（隔离构建 · 齐全后才可切换）</h2>
      <section className="card">
        <h3>触发构建作业</h3>
        <div className="filter-row">
          <select value={selected.rel}
            onChange={(e) => setSelected(PACKAGES.find((p) => p.rel === e.target.value))}>
            {PACKAGES.map((p) => <option key={p.rel} value={p.rel}>{p.label}</option>)}
          </select>
          {selected.isbn === '9787100000035' && (
            <label className="switch">
              <input type="checkbox" checked={partial}
                onChange={(e) => setPartial(e.target.checked)} />
              仅提交第 13 章（模拟构建中断，第 14 章缺失）
            </label>
          )}
          <button className="btn primary" onClick={runBuild}>开始构建</button>
        </div>
        {msg && <div className="notice">{msg}</div>}
      </section>

      <section className="card">
        <h3>代次列表</h3>
        <table className="grid">
          <thead>
            <tr><th>代次</th><th>教材</th><th>状态</th><th>章节(成功/总数)</th>
              <th>单元/词项/ postings</th><th>操作</th></tr>
          </thead>
          <tbody>
            {gens.map((g) => (
              <tr key={g.generation_id}>
                <td>{g.generation_id}</td>
                <td>{g.title}<br /><span className="muted small">{g.isbn} · 第{g.edition_generation}版</span></td>
                <td><span className={`badge gen-${g.status}`}>{g.status}</span>
                  {g.error && <div className="muted small">{g.error}</div>}</td>
                <td>{g.ok_chapters}/{g.total_chapters}（失败 {g.failed_chapters}）</td>
                <td>{g.total_units}/{g.total_terms}/{g.total_postings}</td>
                <td>
                  <button className="btn ghost" onClick={() => showReport(g.generation_id)}>逐章报告</button>
                  {g.status === 'archived' &&
                    <button className="btn ghost danger" onClick={() => retire(g.generation_id)}>退役代次</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {report && (
        <section className="card">
          <h3>代次 {report.generation.generation_id} 的逐章阶段报告</h3>
          <table className="grid">
            <thead><tr><th>章节</th><th>阶段</th><th>状态</th><th>数量/错误</th></tr></thead>
            <tbody>
              {report.stage_reports.map((r, i) => (
                <tr key={i}>
                  <td>{r.chapter_code}</td><td>{r.stage}</td>
                  <td><span className={`badge ${r.status === 'ok' ? 'ok' : 'bad'}`}>{r.status}</span></td>
                  <td>{r.error || JSON.stringify(r.counts)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}

      <section className="card">
        <h3>待修正队列（缺图表 / 重复标识 / 缺前提 / 坏章）</h3>
        {issues.length === 0 ? <div className="muted">没有待处理问题。</div> : (
          <table className="grid">
            <thead><tr><th>类型</th><th>章节</th><th>单元</th><th>说明</th><th>操作</th></tr></thead>
            <tbody>
              {issues.map((i) => (
                <tr key={i.issue_id}>
                  <td>{i.issue_type}</td><td>{i.chapter_code}</td><td>{i.unit_code}</td>
                  <td>{i.detail}</td>
                  <td>
                    {i.issue_type === 'chapter_error' &&
                      <button className="btn ghost" onClick={() => submitCorrection(i)}>提交修正稿</button>}
                    <button className="btn ghost" onClick={() => resolveIssue(i.issue_id)}>标记已修正</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </div>
  )
}
