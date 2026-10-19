import { useState, useEffect, useEffectEvent } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { PluggyConnect } from 'react-pluggy-connect'
import { connections } from '@/lib/api'
import { extractApiError } from '@/lib/api-errors'
import { invalidateFinancialQueries } from '@/lib/invalidate-queries'
import { toast } from 'sonner'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'

interface BankConnectDialogProps {
  open: boolean
  onClose: () => void
  reconnectConnectionId?: string
  updateItemId?: string
  provider?: string
  supportsAssetSync?: boolean
}

export function BankConnectDialog({
  open,
  onClose,
  reconnectConnectionId,
  updateItemId,
  provider = 'pluggy',
  supportsAssetSync = false,
}: BankConnectDialogProps) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const [connectToken, setConnectToken] = useState<string | null>(null)
  const [syncAssets, setSyncAssets] = useState(true)
  const [optionsConfirmed, setOptionsConfirmed] = useState(false)
  const [credentialId, setCredentialId] = useState<string | null>(null)
  // Pluggy: with several Pluggy applications the user picks one before the
  // widget opens (RFC Phase 1, AC-1.5); with one it is used silently. A
  // reconnect always uses the connection's own application.
  const choosesCredential = open && provider === 'pluggy' && !reconnectConnectionId
  const { data: credentials, isLoading: credentialsLoading } = useQuery({
    queryKey: ['pluggy-credentials'],
    queryFn: connections.pluggyCredentials,
    enabled: choosesCredential,
  })
  const needsCredentialPick = choosesCredential && (credentials?.length ?? 0) > 1
  // Only prompt for asset-sync when the provider actually imports holdings.
  const needsInitialOptions = !reconnectConnectionId && (supportsAssetSync || needsCredentialPick)
  const waitingForCredentials = choosesCredential && credentialsLoading

  const onTokenError = useEffectEvent(() => {
    toast.error(t('accounts.connectError'))
    onClose()
  })

  useEffect(() => {
    if (!open) {
      setConnectToken(null)
      setSyncAssets(true)
      setOptionsConfirmed(false)
      setCredentialId(null)
      return
    }

    if (waitingForCredentials) return
    if (needsInitialOptions && !optionsConfirmed) return

    let cancelled = false
    const fetchToken = async () => {
      try {
        const token = reconnectConnectionId
          ? await connections.getReconnectToken(reconnectConnectionId)
          : await connections.getConnectToken(provider, credentialId ?? undefined)
        if (!cancelled) setConnectToken(token)
      } catch {
        if (!cancelled) {
          onTokenError()
        }
      }
    }
    fetchToken()

    return () => { cancelled = true }
  }, [open, reconnectConnectionId, provider, needsInitialOptions, optionsConfirmed, waitingForCredentials, credentialId])

  const handleSuccess = async (data: { item: { id: string } }) => {
    try {
      if (reconnectConnectionId) {
        await connections.sync(reconnectConnectionId)
      } else {
        await connections.handleCallback(
          data.item.id,
          provider,
          undefined,
          supportsAssetSync ? { sync_assets: syncAssets } : undefined,
          undefined,
          credentialId ?? undefined,
        )
      }
      invalidateFinancialQueries(queryClient)
      queryClient.invalidateQueries({ queryKey: ['connections'] })
      toast.success(t('accounts.connected'))
    } catch (err) {
      toast.error(extractApiError(err, t('accounts.connectError')))
    } finally {
      handleClose()
    }
  }

  const handleClose = () => {
    setConnectToken(null)
    onClose()
  }

  if (open && !waitingForCredentials && needsInitialOptions && !optionsConfirmed) {
    return (
      <Dialog open={open} onOpenChange={(v) => !v && handleClose()}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{t('connections.initialSyncSettings')}</DialogTitle>
            <p className="text-sm text-muted-foreground">{t('connections.initialSyncSettingsDesc')}</p>
          </DialogHeader>
          {needsCredentialPick && (
            <div className="space-y-2">
              <Label>{t('connections.pluggyApplication')}</Label>
              <p className="text-xs text-muted-foreground">{t('connections.pluggyApplicationHint')}</p>
              <div className="space-y-1.5" role="radiogroup">
                {credentials!.map((c) => (
                  <label
                    key={c.id}
                    className={`flex items-center gap-3 rounded-lg border p-3 cursor-pointer transition-colors ${
                      credentialId === c.id ? 'border-primary bg-primary/5' : 'border-border hover:bg-muted/50'
                    }`}
                  >
                    <input
                      type="radio"
                      name="pluggy-credential"
                      value={c.id}
                      checked={credentialId === c.id}
                      onChange={() => setCredentialId(c.id)}
                      className="h-4 w-4 text-primary focus:ring-primary"
                    />
                    <span className="text-sm text-foreground">{c.name}</span>
                  </label>
                ))}
              </div>
            </div>
          )}
          {supportsAssetSync && (
          <div className="flex items-start justify-between gap-4 rounded-lg border border-border p-3">
            <div className="space-y-1">
              <Label htmlFor="initial-sync-assets">{t('connections.syncAssets')}</Label>
              <p className="text-xs text-muted-foreground">{t('connections.syncAssetsHint')}</p>
            </div>
            <input
              id="initial-sync-assets"
              type="checkbox"
              checked={syncAssets}
              onChange={(e) => setSyncAssets(e.target.checked)}
              className="mt-1 h-4 w-4 rounded border-border text-primary focus:ring-primary"
            />
          </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={handleClose}>{t('common.cancel')}</Button>
            <Button
              onClick={() => setOptionsConfirmed(true)}
              disabled={needsCredentialPick && !credentialId}
            >
              {t('connections.continueToConnector')}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    )
  }

  if (!open || !connectToken) return null

  return (
    <PluggyConnect
      connectToken={connectToken}
      updateItem={updateItemId}
      onSuccess={handleSuccess}
      onClose={handleClose}
      onError={() => {
        toast.error(t('accounts.connectError'))
        handleClose()
      }}
    />
  )
}
