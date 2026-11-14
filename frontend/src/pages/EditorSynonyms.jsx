import { useEffect, useState } from 'react'
import { api } from '../api.js'

export default function EditorSynonyms() {
  const [items, setItems] = useState([])
  const [form, setForm] = useState({ subject: '数学', term_display: '', synonym_display: '' })
  const [msg, setMsg] = useState('')

  async function load() {
    setItems(await api.get('/api/synonyms?status=all'))
  }
  useEffect(() => { load() }, [])

  async function submit(e) {
    e.preventDefault()
    setMsg('')
    try {
      await api.post('/api/synonyms', form)
      setForm({ ...form, term_display: '', synonym_display: '' })
      setMsg('同义词已提交，状态为待审；审核通过前不会用于任何查询扩展。')
    } catch (err) {
      setMsg(err.message)
    }
    await load()
  }

  async function review(id, status) {
    await api.post(`/api/synonyms/${id}/review`, { status })
    await load()
  }

  return (
    <div className="editor-page">
      <h2>同义词学科范围维护</h2>
      <section className="card">
        <h3>新增同义词（教师也可提交，编辑审核后生效）</h3>
        <form className="syn-form" onSubmit={submit}>
          <select value={form.subject}
            onChange={(e) => setForm({ ...form, subject: e.target.value })}>
            <option>数学</option><option>物理</option>
          </select>
          <input placeholder="术语（如 取值范围）" value={form.term_display}
            onChange={(e) => setForm({ ...form, term_display: e.target.value })} required />
          <input placeholder="同义词（如 定义域）" value={form.synonym_display}
            onChange={(e) => setForm({ ...form, synonym_display: e.target.value })} required />
          <button className="btn primary" type="submit">提交待审</button>
        </form>
        {msg && <div className="notice">{msg}</div>}
        <p className="muted small">
          扩展仅使用每次查询开始时的已审词表快照；教师可在检索页关闭扩展比较结果，系统不递归扩展、不放大短词含义。
        </p>
      </section>

      <section className="card">
        <h3>词表审核</h3>
        <table className="grid">
          <thead><tr><th>学科</th><th>术语</th><th>同义词</th><th>状态</th><th>提交/审核</th><th>操作</th></tr></thead>
          <tbody>
            {items.map((s) => (
              <tr key={s.synonym_id}>
                <td>{s.subject}</td><td>{s.term_display}</td><td>{s.synonym_display}</td>
                <td><span className={`badge syn-${s.status}`}>{s.status}</span></td>
                <td className="muted small">{s.created_by}/{s.reviewed_by || '—'}</td>
                <td>
                  {s.status === 'pending' && <>
                    <button className="btn ghost" onClick={() => review(s.synonym_id, 'approved')}>通过</button>
                    <button className="btn ghost danger" onClick={() => review(s.synonym_id, 'rejected')}>驳回</button>
                  </>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  )
}
