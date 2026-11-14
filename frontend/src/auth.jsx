import { createContext, useContext, useMemo, useState } from 'react'
import { clearSession, getToken, getUser, setSession } from './api.js'

const AuthCtx = createContext(null)

export function AuthProvider({ children }) {
  const [user, setUser] = useState(() => getUser())
  const [token, setToken] = useState(() => getToken())

  const value = useMemo(() => ({
    user,
    token,
    login(tok, u) {
      setSession(tok, u)
      setToken(tok)
      setUser(u)
    },
    logout() {
      clearSession()
      setToken('')
      setUser(null)
    },
  }), [user, token])

  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>
}

export function useAuth() {
  return useContext(AuthCtx)
}
