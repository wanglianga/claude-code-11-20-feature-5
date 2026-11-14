import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import Highlight from '../components/Highlight.jsx'

const emptyResult = {
  session_id: null, hits: [], total: 0, page: 1, page_size: 10,
  query_terms: [], expanded_terms: [], no_hit: false, no_hit_reason: '',
  collection_scope: '', generation_ids: [], filters: {}, normalized_query: '',
}

export default function Search() {
  const [books, setBooks] = useState([])
  const [meta, setMeta] = useState({ subjects: [], stages: [] })
  const [query, setQuery] = useState('函数')
  const [stages, setStages] = useState([])
  const [isbns, setIsbns] = useState([])
  const [includeDeprecated, setIncludeDeprecated] = useState(false)
  const [synonymEnabled, setSynonymEnabled] = useState(true)
  const [page, setPage] = useState(1)
  const [result, setResult] = useState(emptyResult)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [sessionId, setSessionId] = useState(null)
  const [citationMsg, setCitationMsg] = useState('')

  useEffect(() => {
    api.get('/api/textbooks').then(setBooks).catch((e) => setError(e.message))
    api.get('/api/meta').then(setMeta).catch(() => {})
  }, [])

  const activeBooks = useMemo(
    () => books.filter((b) => b.active_generation_id), [books])

  async function runSearch(targetPage = 1, keepSession = false) {
    if (!query.trim()) return
    setLoading(true); setError(''); setCitationMsg('')
    try {
      const body = {
        query: query.trim(),
        stages, isbns,
        include_deprecated: includeDeprecated,
        synonym_enabled: synonymEnabled,
        page: targetPage,
        page_size: 5,
        session_id: keepSession ? sessionId : null,
      }
      const data = await api.post('/api/search', body)
      setResult(data)
      setSessionId(data.session_id)
      setPage(data.page)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  function toggle(arr, setArr, val) {
    setSessionId(null)
    setArr(arr.includes(val) ? arr.filter((x) => x !== val) : [...arr, val])
  }

  async function cite(hit) {
    setCitationMsg('')
    try {
      await api.post('/api/citations', {
        session_id: result.session_id,
        unit_id: hit.unit_id,
        quoted_text: hit.summary,
      })
      setCitationMsg(`已固定引用：${hit.unit_code}（代次 ${hit.generation_id}），可在“引用核查”复核`)
    } catch (e) {
      setCitationMsg(`引用失败：${e.message}`)
    }
  }

  const totalPages = Math.max(1, Math.ceil(result.total / result.page_size))
  const allRanges = (hit) => Object.values(hit.positions).flat()

  return (
    <div className="search-page">
      <section className="card filters">
        <h2>第一步：选择教材范围</h2>
        <div className="filter-row">
          <span className="filter-label">学段</span>
          {meta.stages.map((s) => (
            <button key={s} type="button"
              className={`chip ${stages.includes(s) ? 'on' : ''}`}
              onClick={() => toggle(stages, setStages, s)}>{s}</button>
          ))}
        </div>
        <div className="filter-row">
          <span className="filter-label">教材</span>
          <div className="book-chips">
            {activeBooks.map((b) => (
              <button key={b.isbn} type="button"
                className={`chip wide ${isbns.includes(b.isbn) ? 'on' : ''}`}
                onClick={() => toggle(isbns, setIsbns, b.isbn)}
                title={b.isbn}>
                {b.title}
                {b.has_older && <em className="tag-old">含旧版</em>}
              </button>
            ))}
          </div>
        </div>
        <div className="filter-row">
          <label className="switch">
            <input type="checkbox" checked={includeDeprecated}
              onChange={(e) => { setIncludeDeprecated(e.target.checked); setSessionId(null) }} />
            显式开启停用旧版（默认只检索现行稿）
          </label>
          <label className="switch">
            <input type="checkbox" checked={synonymEnabled}
              onChange={(e) => { setSynonymEnabled(e.target.checked); setSessionId(null) }} />
            同义词扩展（仅使用查询开始时的已审词表，可关闭比较）
          </label>
        </div>
        <h2>第二步：输入查询术语</h2>
        <form className="search-bar" onSubmit={(e) => { e.preventDefault(); runSearch(1) }}>
          <input value={query} onChange={(e) => setQuery(e.target.value)}
            placeholder="例如：函数的定义域 / 汽车行驶 / 分子热运动" />
          <button className="btn primary" disabled={loading} type="submit">
            {loading ? '检索中…' : '检索'}
          </button>
        </form>
        {error && <div className="error">{error}</div>}
      </section>

      {result.session_id && (
        <section className="card evidence">
          <div className="ev-head">
            <strong>本次检索的匹配依据（可解释、可本地复现）</strong>
            <span className="muted small">固定索引代次：{result.generation_ids.join(', ') || '无'}</span>
          </div>
          <div className="muted small">范围：{result.collection_scope} ｜ 归一化查询：{result.normalized_query}</div>
          <div className="term-list">
            {result.expanded_terms.map((t, i) => (
              <span key={i} className={`term ${t.source}`}>
                {t.term}
                <sub>{t.source === 'synonym' ? `同义词←${t.via}` : '原词'}</sub>
                <sub>df={t.df} idf={t.idf} 命中{t.matched_units}</sub>
              </span>
            ))}
          </div>
          {result.no_hit && (
            <div className="no-hit">
              <strong>未命中，系统不会生成教材之外的答案。</strong>
              <div>{result.no_hit_reason}</div>
            </div>
          )}
        </section>
      )}

      <section className="hits">
        {result.hits.map((hit) => (
          <article className="card hit" key={hit.unit_id}>
            <div className="hit-head">
              <span className="rank">#{hit.rank}</span>
              <div className="path">
                <div className="chapter">{hit.chapter_path}</div>
                <div className="muted small">{hit.title_path} ｜ 稳定标识 {hit.unit_code}
                  ｜ 类型：{hit.unit_type === 'example' ? '例题' : hit.unit_type === 'figure_note' ? '图表说明' : '段落'}</div>
              </div>
              <div className="version">
                <span className={`badge ${hit.edition_status}`}>
                  {hit.edition_status === 'active' ? '现行稿' : `停用旧版·${hit.supersede_reason || '已替代'}`}
                </span>
                <span className="muted small">代次 {hit.generation_id}</span>
                <span className="score">BM25 {hit.score}</span>
              </div>
            </div>
            <p className="hit-text">
              <Highlight text={hit.text} ranges={allRanges(hit)} />
            </p>
            <details>
              <summary>查看同单元摘要与逐词项排序贡献（不跨例题拼接）</summary>
              <div className="muted small">摘要（仅取本单元命中句）：{hit.summary}</div>
              <table className="contrib">
                <thead><tr><th>命中词项</th><th>BM25 贡献</th></tr></thead>
                <tbody>
                  {Object.entries(hit.term_contributions).map(([term, v]) => (
                    <tr key={term}><td>{term}</td><td>{v}</td></tr>
                  ))}
                </tbody>
              </table>
            </details>
            <div className="hit-actions">
              <Link className="btn ghost"
                to={`/units/${hit.unit_id}?sid=${result.session_id}`}>
                跳到原位置查看完整上下文/例题条件
              </Link>
              <button className="btn primary" onClick={() => cite(hit)}>
                固定此命中为引用
              </button>
            </div>
          </article>
        ))}
        {result.session_id && !result.no_hit && result.hits.length === 0 && (
          <div className="card muted">本页无更多结果。</div>
        )}
      </section>

      {result.total > 0 && (
        <div className="pager">
          <button className="btn ghost" disabled={page <= 1 || loading}
            onClick={() => runSearch(page - 1, true)}>上一页</button>
          <span>第 {page} / {totalPages} 页（共 {result.total} 条，翻页沿用同一代次 {result.generation_ids.join(',')}）</span>
          <button className="btn ghost" disabled={page >= totalPages || loading}
            onClick={() => runSearch(page + 1, true)}>下一页</button>
        </div>
      )}
      {citationMsg && <div className="toast">{citationMsg}</div>}
    </div>
  )
}
