import { useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { AlertTriangle, KeyRound, Pencil, Plus, Trash2 } from 'lucide-react'
import { admin as adminApi } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import type { PluggyCredential } from '@/types'

type ApiError = { response?: { data?: { detail?: string | { message?: string } } } }

function errorMessage(err: ApiError, fallback: string): string {
  const detail = err.response?.data?.detail
  if (typeof detail === 'string') return detail
  return detail?.message || fallback
}

/** Admin → Pluggy applications (RFC toledo-features, Phase 1). */
export function PluggyCredentialsSettings() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [editing, setEditing] = useState<PluggyCredential | 'new' | null>(null)
  const [deleting, setDeleting] = useState<PluggyCredential | null>(null)
  const [name, setName] = useState('')
  const [clientId, setClientId] = useState('')
  const [clientSecret, setClientSecret] = useState('')

  const { data: credentials, isLoading } = useQuery({
    queryKey: ['admin', 'pluggy-credentials'],
    queryFn: adminApi.pluggyCredentials,
  })
  const { data: status } = useQuery({
    queryKey: ['admin', 'pluggy-credentials', 'status'],
    queryFn: adminApi.pluggyStatus,
  })

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['admin', 'pluggy-credentials'] })
    queryClient.invalidateQueries({ queryKey: ['pluggy-credentials'] })
  }

  const saveMutation = useMutation({
    mutationFn: () => {
      if (editing === 'new') {
        return adminApi.createPluggyCredential({ name, client_id: clientId, client_secret: clientSecret })
      }
      return adminApi.updatePluggyCredential(editing!.id, {
        name,
        client_id: clientId || undefined,
        client_secret: clientSecret || undefined,
      })
    },
    onSuccess: () => {
      invalidate()
      setEditing(null)
      toast.success(t('admin.pluggy.saved'))
    },
    onError: (err: ApiError) => toast.error(errorMessage(err, t('common.error'))),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => adminApi.deletePluggyCredential(id),
    onSuccess: () => {
      invalidate()
      setDeleting(null)
      toast.success(t('admin.pluggy.deleted'))
    },
    onError: (err: ApiError) => toast.error(errorMessage(err, t('common.error'))),
  })

  const openEditor = (credential: PluggyCredential | 'new') => {
    setEditing(credential)
    setName(credential === 'new' ? '' : credential.name)
    setClientId('')
    setClientSecret('')
  }

  const isNew = editing === 'new'
  const canSave = name.trim() !== '' && (!isNew || (clientId.trim() !== '' && clientSecret.trim() !== ''))

  return (
    <div className="rounded-xl border border-border/60 bg-card overflow-hidden mb-8">
      <div className="px-5 py-4 border-b border-border/40 flex items-start justify-between gap-3">
        <div>
          <div className="flex items-center gap-2 mb-0.5">
            <KeyRound size={15} className="text-muted-foreground" />
            <h3 className="text-sm font-semibold text-foreground">{t('admin.pluggy.title')}</h3>
          </div>
          <p className="text-xs text-muted-foreground">{t('admin.pluggy.subtitle')}</p>
        </div>
        <Button
          size="sm"
          onClick={() => openEditor('new')}
          disabled={status ? !status.encryption_key_configured : false}
        >
          <Plus size={14} className="mr-1" />
          {t('admin.pluggy.add')}
        </Button>
      </div>

      {status && !status.configured && (
        <div className="flex items-start gap-2 px-5 py-3 text-xs bg-amber-500/10 text-amber-700 dark:text-amber-400 border-b border-border/40">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>
            {t('admin.pluggy.notConfigured')}
            {status.reason ? ` ${status.reason}` : ''}
          </span>
        </div>
      )}

      {isLoading ? (
        <div className="p-4 space-y-2">
          <Skeleton className="h-12 w-full rounded-lg" />
        </div>
      ) : !credentials?.length ? (
        <p className="px-5 py-6 text-sm text-muted-foreground">{t('admin.pluggy.empty')}</p>
      ) : (
        <div className="divide-y divide-border/40">
          {credentials.map((c) => (
            <div key={c.id} className="flex items-center gap-4 px-5 py-3.5">
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-foreground truncate">{c.name}</p>
                <p className="text-xs text-muted-foreground">
                  <span className="font-mono">{c.client_id_hint}</span>
                  {' · '}
                  {t('admin.pluggy.connections', { count: c.connection_count })}
                </p>
                {c.last_error && (
                  <p className="text-xs text-destructive mt-0.5 break-words">
                    {t('admin.pluggy.lastError', { error: c.last_error })}
                  </p>
                )}
              </div>
              <button
                type="button"
                onClick={() => openEditor(c)}
                className="p-1.5 rounded-md text-muted-foreground hover:text-foreground hover:bg-muted transition-colors"
                title={t('common.edit')}
                aria-label={t('common.edit')}
              >
                <Pencil size={14} />
              </button>
              <button
                type="button"
                onClick={() => setDeleting(c)}
                className="p-1.5 rounded-md text-muted-foreground/60 hover:text-destructive hover:bg-destructive/10 transition-colors"
                title={t('common.delete')}
                aria-label={t('common.delete')}
              >
                <Trash2 size={14} />
              </button>
            </div>
          ))}
        </div>
      )}

      <Dialog open={editing !== null} onOpenChange={(v) => !v && setEditing(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{isNew ? t('admin.pluggy.addTitle') : t('admin.pluggy.editTitle')}</DialogTitle>
            <DialogDescription>{t('admin.pluggy.dialogDesc')}</DialogDescription>
          </DialogHeader>
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault()
              if (canSave) saveMutation.mutate()
            }}
          >
            <div className="space-y-1.5">
              <Label htmlFor="pluggy-name">{t('admin.pluggy.name')}</Label>
              <Input id="pluggy-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={100} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="pluggy-client-id">{t('admin.pluggy.clientId')}</Label>
              <Input
                id="pluggy-client-id"
                value={clientId}
                onChange={(e) => setClientId(e.target.value)}
                autoComplete="off"
                placeholder={!isNew && editing ? editing.client_id_hint : undefined}
              />
              {!isNew && <p className="text-xs text-muted-foreground">{t('admin.pluggy.keepBlank')}</p>}
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="pluggy-client-secret">{t('admin.pluggy.clientSecret')}</Label>
              <Input
                id="pluggy-client-secret"
                type="password"
                value={clientSecret}
                onChange={(e) => setClientSecret(e.target.value)}
                autoComplete="new-password"
              />
              {!isNew && <p className="text-xs text-muted-foreground">{t('admin.pluggy.keepBlank')}</p>}
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setEditing(null)}>
                {t('common.cancel')}
              </Button>
              <Button type="submit" disabled={!canSave || saveMutation.isPending}>
                {saveMutation.isPending ? t('common.loading') : t('common.save')}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={deleting !== null} onOpenChange={(v) => !v && setDeleting(null)}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t('admin.pluggy.deleteTitle')}</DialogTitle>
            <DialogDescription>
              {deleting && deleting.connection_count > 0
                ? t('admin.pluggy.deleteBlocked', { count: deleting.connection_count })
                : t('admin.pluggy.deleteConfirm', { name: deleting?.name })}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleting(null)}>
              {t('common.cancel')}
            </Button>
            {deleting && deleting.connection_count === 0 && (
              <Button
                variant="destructive"
                onClick={() => deleteMutation.mutate(deleting.id)}
                disabled={deleteMutation.isPending}
              >
                {t('common.delete')}
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
