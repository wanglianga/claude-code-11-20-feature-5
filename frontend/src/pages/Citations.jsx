import { useEffect, useState } from 'react'
import { api } from '../api.js'

const STATUS_LABEL = {
  ok: { text: '一致', cls: 'ok' },
  drifted: { text: '换版漂移', cls: 'warn' },
  edition_unavailable: { text: '原代次不可访问', cls: 'bad' },
}

export default function Citations() {
  const [items, setItems] = useState([])
  const [msg, setMsg] = useState('')

  async function load() {
    setItems(await api.get('/api/citations'))
  }
  useEffect(() => { load() }, [])

  async function recheck(id) {
    setMsg('')
    try {
      const r = await api.post(`/api/citations/${id}/check`)
      setMsg(`引用 #${id} 复核结果：${STATUS_LABEL[r.last_status]?.text || r.last_status} —— ${r.last_detail}`)
      await load()
    } catch (e) {
      setMsg(`复核失败：${e.message}`)
    }
  }

  return (
    <div className="citations-page">
      <h2>我的引用与核查</h2>
      <p className="muted">
        引用固定创建时所见的索引代次与内容指纹；换版后只提示差异，不会把旧引用悄悄指向新段落。
      </p>
      {msg && <div className="card notice">{msg}</div>}
      <div className="cards">
        {items.length === 0 && <div className="card muted">还没有引用，请先在检索页固定命中。</div>}
        {items.map((c) => {
          const st = STATUS_LABEL[c.last_status] || { text: c.last_status, cls: '' }
          return (
            <article className="card citation" key={c.citation_id}>
              <div className="cit-head">
                <strong>引用 #{c.citation_id}</strong>
                <span className={`badge ${st.cls}`}>{st.text}</span>
              </div>
              <blockquote>“{c.quoted_text}”</blockquote>
              <dl className="meta-grid">
                <dt>段落标识</dt><dd>{c.unit_code}</dd>
                <dt>索引代次</dt><dd>{c.generation_id}（版本 {c.edition_id}）</dd>
                <dt>内容指纹</dt><dd><code>{c.content_hash.slice(0, 20)}…</code></dd>
                <dt>出处</dt><dd>{c.locator.section_path || JSON.stringify(c.locator)}</dd>
                <dt>最近核查</dt><dd>{c.last_checked_at || '—'}</dd>
              </dl>
              <div className="muted small">{c.last_detail}</div>
              <button className="btn primary" onClick={() => recheck(c.citation_id)}>
                重新核查原文与指纹
              </button>
            </article>
          )
        })}
      </div>
    </div>
  )
}
