import { createContext, useContext } from "react";

export interface AuthValue {
  authRequired: boolean;
  logout: () => Promise<void>;
}

export const AuthContext = createContext<AuthValue>({ authRequired: false, logout: async () => undefined });

export const useAuth = () => useContext(AuthContext);
