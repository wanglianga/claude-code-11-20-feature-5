import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.jsx'

export default function Login() {
  const [username, setUsername] = useState('teacher1')
  const [error, setError] = useState('')
  const { login } = useAuth()
  const nav = useNavigate()

  async function submit(e) {
    e.preventDefault()
    setError('')
    try {
      const data = await api.post('/api/auth/login', { username: username.trim() })
      login(data.token, data.user)
      nav(data.user.role === 'editor' ? '/editor/build' : '/search')
    } catch (err) {
      setError(err.message)
    }
  }

  return (
    <div className="login-wrap">
      <form className="card login-card" onSubmit={submit}>
        <h1>教材检索与引用核查平台</h1>
        <p className="muted">面向出版社编辑与一线教师的可解释词项检索</p>
        <label>账号
          <select value={username} onChange={(e) => setUsername(e.target.value)}>
            <option value="teacher1">teacher1 · 李老师（教师）</option>
            <option value="editor1">editor1 · 王编辑（编辑）</option>
          </select>
        </label>
        {error && <div className="error">{error}</div>}
        <button className="btn primary" type="submit">登录</button>
        <p className="muted small">
          测试账号：teacher1 / editor1，无需密码（演示环境令牌鉴权）
        </p>
      </form>
    </div>
  )
}
