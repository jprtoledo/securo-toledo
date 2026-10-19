type PendingBadgeTransaction = {
  source: string | null
  status: string | null
}

/** Bank-sync pending state already matches the provider and needs no UI badge. */
export function shouldShowPendingBadge(transaction: PendingBadgeTransaction): boolean {
  return transaction.status === 'pending' && transaction.source !== 'sync'
}

/** The status filter also offers the sync states of RFC toledo-features §4.9
 *  ("Removed by the bank", "Duplicates"), which the API takes as
 *  `sync_state` instead of `status`. */
export const SYNC_STATE_FILTERS = ['removed', 'duplicate'] as const
export type SyncStateFilter = (typeof SYNC_STATE_FILTERS)[number]

export function statusFilterParams(filterStatus: string): {
  status?: string
  sync_state?: SyncStateFilter
} {
  if (!filterStatus) return {}
  if ((SYNC_STATE_FILTERS as readonly string[]).includes(filterStatus)) {
    return { sync_state: filterStatus as SyncStateFilter }
  }
  return { status: filterStatus }
}
