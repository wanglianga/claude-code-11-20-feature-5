import { useEffect, useState } from 'react'
import { api } from '../api.js'

const STATUS = {
  pending: '待批准', approved: '已批准（现行）', rejected: '已拒绝',
}

export default function EditorReplacements() {
  const [requests, setRequests] = useState([])
  const [books, setBooks] = useState([])
  const [msg, setMsg] = useState('')
  const [form, setForm] = useState({ isbn: '', old_edition_id: null, new_edition_id: null, reason: '' })

  async function load() {
    setRequests(await api.get('/api/admin/replacement'))
    setBooks(await api.get('/api/textbooks'))
  }
  useEffect(() => { load() }, [])

  async function approve(id) {
    setMsg('')
    try {
      const r = await api.post(`/api/admin/replacement/${id}/approve`)
      setMsg(`已原子切换现行索引：${r.isbn} 当前代次 ${r.active_generation_id}；旧版已停用归档。`)
    } catch (e) {
      setMsg(`切换被拒绝：${e.message}`)
    }
    await load()
  }

  async function reject(id) {
    await api.post(`/api/admin/replacement/${id}/reject`, {})
    await load()
  }

  async function submit(e) {
    e.preventDefault()
    setMsg('')
    try {
      await api.post('/api/admin/replacement/request', form)
      setMsg('替代申请已登记，等待批准。')
    } catch (err) {
      setMsg(err.message)
    }
    await load()
  }

  const selectedBook = books.find((b) => b.isbn === form.isbn)

  return (
    <div className="editor-page">
      <h2>教材替代关系（编辑批准制）</h2>
      <section className="card">
        <h3>登记替代申请</h3>
        <form className="syn-form" onSubmit={submit}>
          <select required value={form.isbn}
            onChange={(e) => setForm({ ...form, isbn: e.target.value, old_edition_id: null, new_edition_id: null })}>
            <option value="">选择教材</option>
            {books.map((b) => <option key={b.isbn} value={b.isbn}>{b.title}（{b.isbn}）</option>)}
          </select>
          <select value={form.old_edition_id ?? ''}
            onChange={(e) => setForm({ ...form, old_edition_id: e.target.value ? Number(e.target.value) : null })}>
            <option value="">旧版本（可空=首次启用）</option>
            {selectedBook?.editions.map((e) => (
              <option key={e.edition_id} value={e.edition_id}>第{e.generation}版 · {e.status}</option>
            ))}
          </select>
          <select required value={form.new_edition_id ?? ''}
            onChange={(e) => setForm({ ...form, new_edition_id: Number(e.target.value) })}>
            <option value="">新版本</option>
            {selectedBook?.editions.map((e) => (
              <option key={e.edition_id} value={e.edition_id}>第{e.generation}版 · {e.status}</option>
            ))}
          </select>
          <input placeholder="停用/替代原因" value={form.reason}
            onChange={(e) => setForm({ ...form, reason: e.target.value })} />
          <button className="btn primary" type="submit">登记</button>
        </form>
        {msg && <div className="notice">{msg}</div>}
      </section>

      <section className="card">
        <h3>替代申请与切换</h3>
        <p className="muted small">
          默认检索现行稿，查看旧版需教师显式开启；切换在单个事务内完成，
          失败/不齐全的代次与陈旧作业都无法覆盖更新的新稿。
        </p>
        <table className="grid">
          <thead><tr><th>#</th><th>ISBN</th><th>旧版→新版</th><th>原因</th><th>状态</th><th>操作</th></tr></thead>
          <tbody>
            {requests.map((r) => (
              <tr key={r.request_id}>
                <td>{r.request_id}</td>
                <td className="small">{r.isbn}</td>
                <td>{r.old_edition_id ?? '—'} → {r.new_edition_id}</td>
                <td>{r.reason || '—'}</td>
                <td><span className={`badge req-${r.status}`}>{STATUS[r.status]}</span></td>
                <td>
                  {r.status === 'pending' && <>
                    <button className="btn primary" onClick={() => approve(r.request_id)}>批准并切换</button>
                    <button className="btn ghost danger" onClick={() => reject(r.request_id)}>拒绝</button>
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
