import { Navigate, NavLink, Route, Routes } from 'react-router-dom'
import { useAuth } from './auth.jsx'
import Login from './pages/Login.jsx'
import Search from './pages/Search.jsx'
import UnitView from './pages/UnitView.jsx'
import Citations from './pages/Citations.jsx'
import EditorBuild from './pages/EditorBuild.jsx'
import EditorSynonyms from './pages/EditorSynonyms.jsx'
import EditorReplacements from './pages/EditorReplacements.jsx'
import EvalQuerySets from './pages/EvalQuerySets.jsx'
import EvalRuns from './pages/EvalRuns.jsx'

function Layout({ children }) {
  const { user, logout } = useAuth()
  const tabs = [
    { to: '/search', label: '教材检索', roles: ['teacher', 'editor'] },
    { to: '/citations', label: '引用核查', roles: ['teacher', 'editor'] },
    { to: '/eval/query-sets', label: '评测查询集', roles: ['teacher', 'editor'] },
    { to: '/eval/runs', label: '配置评测', roles: ['teacher', 'editor'] },
    { to: '/editor/build', label: '构建监控', roles: ['editor'] },
    { to: '/editor/synonyms', label: '同义词审核', roles: ['editor'] },
    { to: '/editor/replacements', label: '版本替代', roles: ['editor'] },
  ]
  return (
    <div className="layout">
      <header className="topbar">
        <div className="brand">教材检索与引用核查平台</div>
        <nav>
          {tabs.filter((t) => t.roles.includes(user?.role)).map((t) => (
            <NavLink key={t.to} to={t.to}
              className={({ isActive }) => (isActive ? 'nav active' : 'nav')}>
              {t.label}
            </NavLink>
          ))}
        </nav>
        <div className="userbox">
          <span>{user?.display_name}（{user?.role === 'editor' ? '编辑' : '教师'}）</span>
          <button className="btn ghost" onClick={logout}>退出</button>
        </div>
      </header>
      <main className="content">{children}</main>
    </div>
  )
}

function Protected({ children, editorOnly }) {
  const { user } = useAuth()
  if (!user) return <Navigate to="/login" replace />
  if (editorOnly && user.role !== 'editor') return <Navigate to="/search" replace />
  return <Layout>{children}</Layout>
}

export default function App() {
  const { user } = useAuth()
  return (
    <Routes>
      <Route path="/login" element={user ? <Navigate to="/search" /> : <Login />} />
      <Route path="/search" element={<Protected><Search /></Protected>} />
      <Route path="/units/:id" element={<Protected><UnitView /></Protected>} />
      <Route path="/citations" element={<Protected><Citations /></Protected>} />
      <Route path="/eval/query-sets" element={<Protected><EvalQuerySets /></Protected>} />
      <Route path="/eval/runs" element={<Protected><EvalRuns /></Protected>} />
      <Route path="/editor/build" element={<Protected editorOnly><EditorBuild /></Protected>} />
      <Route path="/editor/synonyms" element={<Protected editorOnly><EditorSynonyms /></Protected>} />
      <Route path="/editor/replacements" element={<Protected editorOnly><EditorReplacements /></Protected>} />
      <Route path="*" element={<Navigate to={user ? '/search' : '/login'} replace />} />
    </Routes>
  )
}
