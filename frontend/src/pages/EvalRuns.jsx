import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'
import Highlight from '../components/Highlight.jsx'

const TYPE_LABEL = { concept: '常见概念', required_chapter: '必考章节', example: '例题题型' }
const METRIC_LABEL = {
  recall_at_k: '前列召回 Recall@K',
  ndcg_at_k: 'NDCG@K',
  no_result_rate: '无结果比例',
}

export default function EvalRuns() {
  const { user } = useAuth()
  const isEditor = user?.role === 'editor'
  const [sets, setSets] = useState([])
  const [configs, setConfigs] = useState([])
  const [runs, setRuns] = useState([])
  const [detail, setDetail] = useState(null)
  const [msg, setMsg] = useState('')
  const [form, setForm] = useState({
    set_id: '', baseline_config_id: '', candidate_config_id: '',
    seed: 20261114, bootstrap_iters: 1000, top_k: 10,
    eval_timeout_ms: 2000, batch_size: 20,
  })

  async function load() {
    setSets(await api.get('/api/eval/query-sets'))
    setConfigs(await api.get('/api/eval/configs'))
    setRuns(await api.get('/api/eval/runs'))
  }
  useEffect(() => { load() }, [])

  async function openRun(id) {
    setDetail(await api.get(`/api/eval/runs/${id}`))
  }

  async function createRun(e) {
    e.preventDefault()
    setMsg('')
    try {
      const r = await api.post('/api/eval/runs', {
        ...form, set_id: Number(form.set_id),
        baseline_config_id: Number(form.baseline_config_id),
        candidate_config_id: Number(form.candidate_config_id),
      })
      setMsg(`评测已创建：${r.status}，完成 ${r.done_queries}/${r.total_queries}`)
      await load()
      await openRun(r.run_id)
    } catch (err) { setMsg(err.message) }
  }

  async function action(id, path, body) {
    const r = await api.post(`/api/eval/runs/${id}/${path}`, body || {})
    await load()
    if (detail && detail.run.run_id === id) await openRun(id)
    else setMsg(`运行 ${id} 状态：${r.status}`)
  }

  return (
    <div className="eval-page">
      <h2>候选配置对比评测（相同教材内容与查询集 · 固定输入可复现）</h2>
      {msg && <div className="notice">{msg}</div>}

      <ConfigPanel configs={configs} isEditor={isEditor} onChanged={load} />

      {isEditor && (
        <section className="card">
          <h3>发起对比评测（两套配置运行在同一冻结查询集上）</h3>
          <form className="syn-form" onSubmit={createRun}>
            <select value={form.set_id} onChange={(e) => setForm({ ...form, set_id: e.target.value })} required>
              <option value="">选择冻结查询集</option>
              {sets.filter((s) => s.status === 'frozen').map((s) =>
                <option key={s.set_id} value={s.set_id}>{s.set_id} · {s.name}（{s.query_count} 条）</option>)}
            </select>
            <select value={form.baseline_config_id}
              onChange={(e) => setForm({ ...form, baseline_config_id: e.target.value })} required>
              <option value="">基线配置</option>
              {configs.map((c) => <option key={c.config_id} value={c.config_id}>{c.name}</option>)}
            </select>
            <select value={form.candidate_config_id}
              onChange={(e) => setForm({ ...form, candidate_config_id: e.target.value })} required>
              <option value="">候选配置</option>
              {configs.map((c) => <option key={c.config_id} value={c.config_id}>{c.name}</option>)}
            </select>
            <label className="small">种子 <input type="number" value={form.seed}
              onChange={(e) => setForm({ ...form, seed: Number(e.target.value) })} /></label>
            <label className="small">重采样次数 <input type="number" value={form.bootstrap_iters}
              onChange={(e) => setForm({ ...form, bootstrap_iters: Number(e.target.value) })} /></label>
            <label className="small">Top K <input type="number" value={form.top_k}
              onChange={(e) => setForm({ ...form, top_k: Number(e.target.value) })} /></label>
            <label className="small">超时(ms) <input type="number" value={form.eval_timeout_ms}
              onChange={(e) => setForm({ ...form, eval_timeout_ms: Number(e.target.value) })} /></label>
            <label className="small">批次大小 <input type="number" value={form.batch_size}
              onChange={(e) => setForm({ ...form, batch_size: Number(e.target.value) })} /></label>
            <button className="btn primary" type="submit">开始评测</button>
          </form>
        </section>
      )}

      <section className="card">
        <h3>评测运行列表（超时/失败查询保留在完成统计中）</h3>
        <table className="grid">
          <thead><tr><th>ID</th><th>查询集</th><th>基线 → 候选</th><th>状态/进度</th>
            <th>成功/超时/失败</th><th>采用决策</th><th>操作</th></tr></thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.run_id}>
                <td>{r.run_id}</td>
                <td>{r.set_name}</td>
                <td className="small">{r.baseline_name}<br />→ {r.candidate_name}</td>
                <td><span className={`badge gen-${r.status === 'completed' ? 'ready' : r.status === 'paused' ? 'archived' : 'building'}`}>{r.status}</span>
                  <div className="muted small">{r.done_queries}/{r.total_queries}</div></td>
                <td className="small">{r.ok_queries} / {r.timeout_queries} / {r.error_queries}</td>
                <td>{r.adoption_decision
                  ? <span className={`badge syn-${r.adoption_decision === 'adopted' ? 'approved' : 'rejected'}`}>{r.adoption_decision}</span>
                  : <span className="muted">未决定</span>}</td>
                <td>
                  <button className="btn ghost" onClick={() => openRun(r.run_id)}>查看报告</button>
                  {isEditor && r.status === 'running' &&
                    <button className="btn ghost" onClick={() => action(r.run_id, 'pause')}>暂停</button>}
                  {isEditor && r.status === 'paused' &&
                    <button className="btn primary" onClick={() => action(r.run_id, 'resume', { batch_size: 20 })}>续接</button>}
                  {isEditor && r.status === 'running' &&
                    <button className="btn ghost" onClick={() => action(r.run_id, 'process', { batch_size: 20 })}>跑一批</button>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>

      {detail && <RunReport detail={detail} isEditor={isEditor} onChanged={() => openRun(detail.run.run_id)} />}
    </div>
  )
}

function ConfigPanel({ configs, isEditor, onChanged }) {
  const [form, setForm] = useState({ name: '', description: '', settings: {
    bm25_k1: 1.5, bm25_b: 0.75, synonym_enabled: true, bigram_enabled: true, top_k: 10 } })
  const [reports, setReports] = useState({})

  async function create(e) {
    e.preventDefault()
    await api.post('/api/eval/configs', form)
    setForm({ ...form, name: '', description: '' })
    onChanged()
  }
  async function check(id) {
    const r = await api.post(`/api/eval/configs/${id}/compatibility`)
    setReports({ ...reports, [id]: r })
    onChanged()
  }
  async function publish(id) {
    try {
      await api.post(`/api/eval/configs/${id}/publish`)
      onChanged()
    } catch (e) { window.alert(e.message) }
  }
  function update(key, val) {
    setForm({ ...form, settings: { ...form.settings, [key]: val } })
  }
  return (
    <section className="card">
      <h3>候选配置（切分/排序参数；仅作用于评测，不改变普通查询）</h3>
      {isEditor && (
        <form className="syn-form" onSubmit={create}>
          <input placeholder="配置名称" value={form.name}
            onChange={(e) => setForm({ ...form, name: e.target.value })} required />
          <label className="small">k1 <input type="number" step="0.1" value={form.settings.bm25_k1}
            onChange={(e) => update('bm25_k1', Number(e.target.value))} /></label>
          <label className="small">b <input type="number" step="0.05" value={form.settings.bm25_b}
            onChange={(e) => update('bm25_b', Number(e.target.value))} /></label>
          <label className="switch"><input type="checkbox" checked={form.settings.synonym_enabled}
            onChange={(e) => update('synonym_enabled', e.target.checked)} />同义词扩展</label>
          <label className="switch"><input type="checkbox" checked={form.settings.bigram_enabled}
            onChange={(e) => update('bigram_enabled', e.target.checked)} />二元组切分</label>
          <button className="btn primary" type="submit">新建配置</button>
        </form>
      )}
      <table className="grid" style={{ marginTop: 10 }}>
        <thead><tr><th>ID</th><th>名称</th><th>参数</th><th>状态</th><th>兼容核对/发布</th></tr></thead>
        <tbody>
          {configs.map((c) => (
            <tr key={c.config_id}>
              <td>{c.config_id}</td><td>{c.name}<div className="muted small">{c.description}</div></td>
              <td className="small"><code>{JSON.stringify(c.settings)}</code></td>
              <td><span className="badge syn-pending">{c.status}</span></td>
              <td>
                <button className="btn ghost" onClick={() => check(c.config_id)}>核对当前索引</button>
                {isEditor && c.status !== 'published' &&
                  <button className="btn primary" onClick={() => publish(c.config_id)}>发布</button>}
                {reports[c.config_id] && (
                  <div className={reports[c.config_id].compatible ? 'notice' : 'no-hit'}>
                    {reports[c.config_id].compatible
                      ? `兼容：${reports[c.config_id].generations.length} 个索引代次可用`
                      : `不兼容：${reports[c.config_id].problems.join('；')}`}
                  </div>
                )}
                {c.compat_report && c.compat_report.checked_at && !reports[c.config_id] && (
                  <div className="muted small">上次核对：{c.compat_report.compatible ? '兼容' : '不兼容'}</div>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  )
}

function RunReport({ detail, isEditor, onChanged }) {
  const { run, summary, per_query } = detail
  const [filter, setFilter] = useState('all')
  const [decisionMsg, setDecisionMsg] = useState('')

  async function decide(decision) {
    const note = window.prompt(
      summary.core_regression && decision === 'adopted'
        ? '核心分组存在退步！请填写人工仍采用的理由：' : '决策备注（可空）：', '')
    if (note === null) return
    try {
      await api.post(`/api/eval/runs/${run.run_id}/decision`, { decision, note })
      setDecisionMsg('采用决策已登记并留痕')
      onChanged()
    } catch (e) { setDecisionMsg(e.message) }
  }

  const filtered = per_query.filter((q) => filter === 'all'
    || (filter === 'regressed' && (q.delta.ndcg_at_k ?? 0) < 0)
    || (filter === 'improved' && (q.delta.ndcg_at_k ?? 0) > 0)
    || (filter === 'hard' && q.hard_failure))
  const hasSummary = summary && summary.ci && summary.groups

  return (
    <>
      {run.status !== 'completed' && (
        <section className="card">
          <h3>运行进行中（可暂停后续接，已完成的查询结果不会重算）</h3>
          <div className="notice">当前状态：{run.status}，进度 {run.done_queries}/{run.total_queries}；
            成功 {run.ok_queries} · 超时 {run.timeout_queries} · 失败/无范围 {run.error_queries}（难例保留）。
            汇总指标与差异区间将在全部查询完成后生成。</div>
        </section>
      )}
      {hasSummary && (
      <section className="card">
        <h3>运行 {run.run_id} 报告
          <span className="muted small" style={{ marginLeft: 10 }}>
            快照 {run.snapshot.frozen_token.slice(0, 8)}… · 固定 {run.snapshot.generation_count} 个索引代次 ·
            种子 {run.seed} · {run.snapshot.baseline_config.name} → {run.snapshot.candidate_config.name}
          </span>
        </h3>
        {summary.warnings && summary.warnings.length > 0 && (
          <div className="no-hit"><strong>需要人工决定是否采用：</strong>
            <ul>{summary.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul></div>
        )}
        {decisionMsg && <div className="notice">{decisionMsg}</div>}
        {isEditor && run.status === 'completed' && (
          <div className="filter-row">
            <button className="btn primary" onClick={() => decide('adopted')}>人工采用候选配置</button>
            <button className="btn ghost danger" onClick={() => decide('rejected')}>拒绝采用</button>
            {run.decision_note && <span className="muted small">决策留痕：{run.decision_note}</span>}
          </div>
        )}
        <OverallTable summary={summary} />
      </section>
      )}

      {hasSummary && (
      <section className="card">
        <h3>按册次 × 题型分组（样本不足只报计数，不宣布改进）</h3>
        <table className="grid">
          <thead><tr><th>册次</th><th>题型</th><th>样本</th><th>核心组</th>
            <th>基线 NDCG</th><th>候选 NDCG</th><th>差异区间（固定种子成对重采样）</th>
            <th>基线无结果率</th><th>候选无结果率</th></tr></thead>
          <tbody>
            {summary.groups.map((g, i) => {
              const ci = g.ci.ndcg_at_k
              return (
                <tr key={i} className={g.core_group && ci.point < 0 ? 'row-bad' : ''}>
                  <td>{g.volume || '未分册'}</td>
                  <td>{TYPE_LABEL[g.question_type] || g.question_type}</td>
                  <td>{g.n_total}（有效 {g.baseline.n_scored}）</td>
                  <td>{g.core_group ? <span className="badge req">含必需出处</span> : '—'}</td>
                  <td>{g.baseline.ndcg_at_k ?? '—'}</td>
                  <td>{g.candidate.ndcg_at_k ?? '—'}</td>
                  <td>
                    <DeltaCI ci={ci} />
                    {!g.sufficient_sample && <div className="muted small">⚠ {g.note}</div>}
                  </td>
                  <td>{g.baseline.no_result_rate ?? '—'}</td>
                  <td>{g.candidate.no_result_rate ?? '—'}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </section>
      )}

      <section className="card">
        <h3>逐条查询升降（可展开原文与排名贡献定位原因）</h3>
        <div className="filter-row">
          {[['all', '全部'], ['regressed', '仅退步'], ['improved', '仅改善'], ['hard', '仅难例/失败']].map(([k, v]) =>
            <button key={k} className={`chip ${filter === k ? 'on' : ''}`} onClick={() => setFilter(k)}>{v}</button>)}
        </div>
        <table className="grid">
          <thead><tr><th>查询</th><th>题型/册次</th><th>状态（基/候）</th>
            <th>NDCG 差</th><th>召回差</th><th>无结果差</th><th></th></tr></thead>
          <tbody>
            {filtered.map((q) => (
              <QueryDetailRow key={q.query_id} q={q} run={run} />
            ))}
          </tbody>
        </table>
      </section>
    </>
  )
}

function OverallTable({ summary }) {
  return (
    <table className="grid">
      <thead><tr><th>指标</th><th>基线均值</th><th>候选均值</th>
        <th>成对差异点估计</th><th>95% 区间（方法/样本量）</th><th>结论</th></tr></thead>
      <tbody>
        {['recall_at_k', 'ndcg_at_k', 'no_result_rate'].map((key) => {
          const ci = summary.ci[key]
          return (
            <tr key={key}>
              <td>{METRIC_LABEL[key]}</td>
              <td>{summary.baseline[key] ?? '—'}</td>
              <td>{summary.candidate[key] ?? '—'}</td>
              <td className={ci.point > 0 ? 'delta-up' : ci.point < 0 ? 'delta-down' : ''}>
                {ci.point > 0 ? '+' : ''}{ci.point}</td>
              <td className="small">[{ci.ci_low ?? '—'}, {ci.ci_high ?? '—'}]
                <div className="muted">{ci.method}</div></td>
              <td><DeltaCI ci={ci} /></td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}

function DeltaCI({ ci }) {
  if (ci.direction === 'sample_too_small')
    return <span className="muted small">样本不足，不宣布改进</span>
  if (!ci.significant)
    return <span className="badge gen-archived">区间跨零，不能称显著获胜</span>
  return <span className={`badge ${ci.direction === 'candidate_better' ? 'ok' : 'bad'}`}>
    {ci.direction === 'candidate_better' ? '候选显著更好' : '候选显著更差'}
  </span>
}

function QueryDetailRow({ q, run }) {
  const [open, setOpen] = useState(false)
  const b = q.baseline, c = q.candidate
  return (
    <>
      <tr className={q.hard_failure ? 'row-bad' : ''}>
        <td><strong>{q.query_text}</strong>
          <div className="muted small">相关条目：{q.relevance.map((r, i) =>
            <span key={i} className="chip" style={{ margin: 2 }}>
              {r.units.join('=')}（{r.grade}级{r.required ? '·必需' : ''}）
            </span>)}</div></td>
        <td className="small">{TYPE_LABEL[q.question_type]}<br />{q.volume}</td>
        <td><StatusBadge s={b.status} /><StatusBadge s={c.status} />
          {(b.error || c.error) && <div className="muted small">{b.error || c.error}</div>}</td>
        <td className={q.delta.ndcg_at_k > 0 ? 'delta-up' : q.delta.ndcg_at_k < 0 ? 'delta-down' : ''}>
          {q.delta.ndcg_at_k ?? '—'}</td>
        <td className={q.delta.recall_at_k > 0 ? 'delta-up' : q.delta.recall_at_k < 0 ? 'delta-down' : ''}>
          {q.delta.recall_at_k ?? '—'}</td>
        <td>{q.delta.no_result ?? '—'}</td>
        <td><button className="btn ghost" onClick={() => setOpen(!open)}>
          {open ? '收起' : '展开原文与排名贡献'}</button></td>
      </tr>
      {open && (
        <tr>
          <td colSpan={7}>
            <div className="side-compare">
              <SideHits title="基线配置" side={b} relevance={q.relevance} />
              <SideHits title="候选配置" side={c} relevance={q.relevance} />
            </div>
          </td>
        </tr>
      )}
    </>
  )
}

function StatusBadge({ s }) {
  const cls = s === 'ok' ? 'ok' : s === 'timeout' ? 'warn' : 'bad'
  return <span className={`badge ${cls}`} style={{ marginRight: 4 }}>{s}</span>
}

function SideHits({ title, side, relevance }) {
  const relUnits = new Set(relevance.flatMap((r) => r.units))
  return (
    <div className="side-col">
      <h4>{title}（{side.status}，{side.elapsed_ms}ms）
        {side.metrics && side.metrics.ndcg_at_k !== undefined &&
          <span className="muted small"> NDCG={side.metrics.ndcg_at_k} 召回={side.metrics.recall_at_k}</span>}</h4>
      {side.ranked_hits.length === 0 && <div className="no-hit">无结果（保留在统计中，不删除）</div>}
      {side.ranked_hits.map((h) => (
        <details key={h.unit_id} className={relUnits.has(h.unit_code) ? 'hit-relevant' : ''}>
          <summary>
            #{h.rank} <code>{h.unit_code}</code> {h.title_path}
            <span className="score">BM25 {h.score}</span>
            {relUnits.has(h.unit_code) && <span className="badge req">标注相关</span>}
          </summary>
          <p className="hit-text"><Highlight text={h.text}
            ranges={Object.values(h.positions).flat()} /></p>
          <table className="contrib">
            <thead><tr><th>命中词项</th><th>BM25 贡献</th></tr></thead>
            <tbody>{Object.entries(h.term_contributions).map(([t, v]) =>
              <tr key={t}><td>{t}</td><td>{v}</td></tr>)}</tbody>
          </table>
        </details>
      ))}
    </div>
  )
}
