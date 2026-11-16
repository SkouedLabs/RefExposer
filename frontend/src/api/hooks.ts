import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { notifications } from '@mantine/notifications';
import { api, setPublicBase } from './client';
import type { ReferentialSummary } from './types';

const anyRunning = (refs?: ReferentialSummary[]) => refs?.some((r) => r.current_run) ?? false;

/**
 * The referential list: one query for the whole application (the sidebar, the dashboard, the administration),
 * so they share the same data and the same refresh.
 *
 * The interval follows the size of the answer. A referential carries about a kilobyte of state, so a few
 * hundred of them make this the heaviest thing the interface asks for, and it is asked for again on a timer:
 * polling a large instance every two seconds leaves the browser parsing and re-rendering more often than a
 * person can read. Past `BIG` referentials the pace is halved while an update is running, and doubled when
 * nothing is.
 */
const BIG = 150;

export function useReferentials() {
  return useQuery({
    queryKey: ['referentials'],
    queryFn: api.referentials,
    refetchInterval: (q) => {
      const big = (q.state.data?.length ?? 0) > BIG;
      if (anyRunning(q.state.data)) return big ? 5000 : 2000;
      return big ? 30000 : 15000;
    },
  });
}

/**
 * The alert feed. One query for the whole application, so the bell in the header and the alerts page share
 * the same data and the same refresh.
 *
 * `counts.unread` is what the account has not marked as read, kept on the server: the badge of the bell says
 * the same thing on every browser the person uses, and an alert read elsewhere stops counting here.
 */
export function useAlerts(limit = 50, dismissed = false) {
  return useQuery({
    queryKey: ['alerts', limit, dismissed],
    queryFn: () => api.alerts(limit, undefined, dismissed),
    refetchInterval: 60_000,
  });
}

/**
 * Availability of the relational search. It is an **experimental feature**, off until an administrator turns
 * it on, so the menu entry, the route and the assistant tools all follow this one answer rather than letting
 * the page fail on an endpoint that is not there.
 */
export function useInvestigate() {
  return useQuery({ queryKey: ['investigate-status'], queryFn: api.investigateStatus, staleTime: 60_000 });
}

/** Availability of the assistant: it only appears in the menu once an administrator enabled it. */
export function useAssistant() {
  return useQuery({ queryKey: ['assistant'], queryFn: api.assistant, staleTime: 60_000 });
}

export function useReferential(id: string, opts: { enabled?: boolean } = {}) {
  return useQuery({
    queryKey: ['referential', id],
    enabled: opts.enabled ?? true,
    queryFn: () => api.referential(id),
    refetchInterval: (q) => (q.state.data?.current_run ? 1500 : 15000),
    retry: (count, err) => (err as { status?: number }).status !== 404 && count < 2,
  });
}

export function useRefresh() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, force, replace }: { id: string; force?: boolean; replace?: boolean }) => api.refresh(id, force, replace),
    onSuccess: (run) => {
      const how = run.replace ? ' (new base)' : run.force ? ' (forced rebuild)' : '';
      notifications.show({ title: 'Update started', message: `${run.ref_id}${how}`, color: 'blue' });
      qc.invalidateQueries({ queryKey: ['referentials'] });
      qc.invalidateQueries({ queryKey: ['referential', run.ref_id] });
      qc.invalidateQueries({ queryKey: ['runs', run.ref_id] });
    },
    onError: (e: Error) => notifications.show({ title: 'Failed', message: e.message, color: 'red' }),
  });
}

export function useCancel() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => api.cancel(id),
    onSuccess: (_, id) => {
      notifications.show({ title: 'Cancellation requested', message: id, color: 'orange' });
      qc.invalidateQueries({ queryKey: ['referential', id] });
    },
    onError: (e: Error) => notifications.show({ title: 'Failed', message: e.message, color: 'red' }),
  });
}

/** Name, logo and public URL of the application (public: also used on the sign-in page). */
export function useBranding() {
  return useQuery({
    queryKey: ['branding'],
    queryFn: async () => {
      const b = await api.branding();
      setPublicBase(b.public_url);
      document.title = b.subtitle ? `${b.title} — ${b.subtitle}` : b.title;
      return b;
    },
    staleTime: 5 * 60_000,
  });
}
