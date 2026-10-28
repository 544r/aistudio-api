import { request } from './client.ts'
import type { AuthCheckResponse, ChangePasswordResponse, StatsResponse } from '@/types'

export const authApi = {
  checkAuth(): Promise<AuthCheckResponse> {
    return request<AuthCheckResponse>('/auth/check')
  },

  verifyToken(token: string): Promise<StatsResponse> {
    return request<StatsResponse>('/stats', {
      headers: {
        Authorization: `Bearer ${token}`,
      },
    })
  },

  changePassword(
    currentPassword: string,
    newPassword: string
  ): Promise<ChangePasswordResponse> {
    return request<ChangePasswordResponse>('/auth/password', {
      method: 'PUT',
      body: JSON.stringify({
        current_password: currentPassword,
        new_password: newPassword,
      }),
    })
  },
}
