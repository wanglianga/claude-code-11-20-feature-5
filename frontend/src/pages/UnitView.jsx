import { useEffect, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'

export default function UnitView() {
  const { id } = useParams()
  const [params] = useSearchParams()
  const sid = params.get('sid')
  const [unit, setUnit] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    api.get(`/api/units/${id}${sid ? `?session_id=${sid}` : ''}`)
      .then(setUnit)
      .catch((e) => setError(e.message))
  }, [id, sid])

  if (error) return <div className="card error">{error}</div>
  if (!unit) return <div className="card muted">加载中…</div>

  return (
    <div className="unit-page">
      <Link className="btn ghost back" to="/search">← 返回检索结果</Link>
      <section className="card">
        <h2>{unit.title_path}</h2>
        <div className="muted small">
          {unit.section_path} ｜ 稳定标识 {unit.unit_code} ｜
          类型：{unit.unit_type === 'example' ? '例题（独立结构单元）' : '段落'} ｜
          索引代次 {unit.generation_id}
        </div>
        <div className="muted small">
          内容指纹（SHA-256）：<code>{unit.content_hash.slice(0, 24)}…</code>
        </div>

        <h3>完整原文（按句切分，保留原段稳定标识，不以摘要代替原文）</h3>
        <div className="full-text">
          {unit.sentence_ranges.map(([s, e], i) => (
            <span key={i} className="sentence" title={`句子区间 [${s}, ${e})`}>
              {unit.text.slice(s, e)}
            </span>
          ))}
        </div>

        {unit.unit_type === 'example' && (
          <>
            <h3>例题前提条件（独立保存，不被摘要切掉）</h3>
            {unit.conditions && unit.conditions.length > 0 ? (
              <ul className="conditions">
                {unit.conditions.map((c, i) => <li key={i}>{c}</li>)}
              </ul>
            ) : (
              <div className="no-hit">该例题未提供前提条件，构建时已进入“待修正（missing_premise）”队列。</div>
            )}
            <div className="notice">
              例题为独立单元，上方为完整例题正文及其前提条件；系统不会把下一节结论拼入本例题。
            </div>
          </>
        )}
      </section>

      <section className="card">
        <h3>本章结构边界（同代次相邻单元，可核对纸书位置）</h3>
        <ol className="siblings">
          {unit.sibling_units.map((s) => (
            <li key={s.unit_id} className={s.unit_id === unit.unit_id ? 'current' : ''}>
              {s.unit_id === unit.unit_id
                ? <strong>{s.unit_code} · {s.title_path}（当前位置）</strong>
                : <Link to={`/units/${s.unit_id}${sid ? `?sid=${sid}` : ''}`}>
                    {s.unit_code} · {s.title_path}
                  </Link>}
            </li>
          ))}
        </ol>
      </section>
    </div>
  )
}
