import {
	Books,
	CheckCircle,
	CircleNotch,
	FolderOpen,
	Warning
} from '@phosphor-icons/react';
import type {Event} from '@sd/ts-client';
import {queryClient} from '@sd/ts-client/hooks';
import {
	Button,
	Dialog,
	dialogManager,
	Input,
	Label,
	useDialog
} from '@spacedrive/primitives';
import {useEffect, useRef, useState} from 'react';
import {useForm} from 'react-hook-form';
import {usePlatform} from '../../contexts/PlatformContext';
import {
	useCoreMutation,
	useSpacedriveClient
} from '../../contexts/SpacedriveContext';

interface CreateLibraryDialogProps {
	id: number;
	onLibraryCreated?: (libraryId: string) => void;
}

interface CreateLibraryFormData {
	name: string;
	path: string | null;
}

type DialogStep = 'form' | 'creating' | 'success' | 'error';

const LIBRARY_CREATE_CLOSE_DELAY_MS = 1500;
const DEFAULT_SUCCESS_DETAIL = 'Finishing setup. This closes automatically.';

/**
 * Hook to open the Create Library dialog
 *
 * @example
 * ```tsx
 * const handleNewLibrary = () => {
 *   useCreateLibraryDialog((libraryId) => {
 *     console.log('Created library:', libraryId);
 *   });
 * };
 * ```
 */
export function useCreateLibraryDialog(
	onLibraryCreated?: (libraryId: string) => void
) {
	return dialogManager.create((props: CreateLibraryDialogProps) => (
		<CreateLibraryDialog {...props} onLibraryCreated={onLibraryCreated} />
	));
}

function CreateLibraryDialog(props: CreateLibraryDialogProps) {
	const dialog = useDialog(props);
	const client = useSpacedriveClient();
	const platform = usePlatform();

	const [step, setStep] = useState<DialogStep>('form');
	const [errorMessage, setErrorMessage] = useState<string | null>(null);
	const [successDetail, setSuccessDetail] = useState(DEFAULT_SUCCESS_DETAIL);

	const createLibrary = useCoreMutation('libraries.create');

	const unsubscribeRef = useRef<(() => void) | null>(null);
	const closeTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
	const pendingLibraryIdRef = useRef<string | null>(null);
	const receivedEventsRef = useRef<
		Array<{id: string; name: string; path: string}>
	>([]);

	const form = useForm<CreateLibraryFormData>({
		defaultValues: {
			name: '',
			path: null
		}
	});

	const cleanupAsyncState = () => {
		if (unsubscribeRef.current) {
			unsubscribeRef.current();
			unsubscribeRef.current = null;
		}

		if (closeTimeoutRef.current) {
			clearTimeout(closeTimeoutRef.current);
			closeTimeoutRef.current = null;
		}
	};

	const closeDialog = () => {
		cleanupAsyncState();
		pendingLibraryIdRef.current = null;
		receivedEventsRef.current = [];
		dialog.state.open = false;
	};

	const scheduleClose = () => {
		if (closeTimeoutRef.current) {
			clearTimeout(closeTimeoutRef.current);
		}

		closeTimeoutRef.current = setTimeout(() => {
			closeDialog();
		}, LIBRARY_CREATE_CLOSE_DELAY_MS);
	};

	// Clean up subscription on unmount
	useEffect(() => {
		return () => {
			cleanupAsyncState();
		};
	}, []);

	const handleBrowse = async () => {
		if (!platform.openDirectoryPickerDialog) {
			console.error('Directory picker not available on this platform');
			return;
		}

		const selected = await platform.openDirectoryPickerDialog({
			title: 'Choose library location',
			multiple: false
		});

		if (selected && typeof selected === 'string') {
			form.setValue('path', selected);
		}
	};

	const onSubmit = form.handleSubmit(async (data) => {
		if (!data.name.trim()) {
			form.setError('name', {
				type: 'manual',
				message: 'Library name is required'
			});
			return;
		}

		setStep('creating');
		setErrorMessage(null);
		setSuccessDetail(DEFAULT_SUCCESS_DETAIL);
		receivedEventsRef.current = [];
		pendingLibraryIdRef.current = null;
		cleanupAsyncState();

		try {
			const unsubscribe = await client.subscribe((event: Event) => {
				if (typeof event !== 'object' || !('LibraryCreated' in event)) {
					return;
				}

				const libraryEvent = event.LibraryCreated;

				if (pendingLibraryIdRef.current === libraryEvent.id) {
					closeDialog();
				} else {
					receivedEventsRef.current.push(libraryEvent);
				}
			});

			unsubscribeRef.current = unsubscribe;
		} catch (err) {
			console.error('Failed to subscribe to events:', err);
		}

		try {
			const result = await createLibrary.mutateAsync({
				name: data.name.trim(),
				path: data.path
			});

			pendingLibraryIdRef.current = result.library_id;

			const alreadyReceived = receivedEventsRef.current.some(
				(e) => e.id === result.library_id
			);

			let postCreateWarning: string | null = null;

			try {
				await queryClient.invalidateQueries({
					queryKey: ['libraries.list']
				});
				await queryClient.invalidateQueries({
					queryKey: ['core.status']
				});
			} catch (error) {
				console.error('Failed to refresh library queries:', error);
				postCreateWarning =
					'Library created, but the library list may need a refresh.';
			}

			try {
				if (platform.setCurrentLibraryId) {
					await platform.setCurrentLibraryId(result.library_id);
				} else {
					client.setCurrentLibrary(result.library_id);
				}
			} catch (error) {
				console.error(
					'Failed to switch libraries after create:',
					error
				);
				client.setCurrentLibrary(result.library_id);
				postCreateWarning =
					'Library created, but automatic switching failed. Use the library switcher if needed.';
			}

			if (props.onLibraryCreated) {
				try {
					props.onLibraryCreated(result.library_id);
				} catch (error) {
					console.error(
						'Failed to run library created callback:',
						error
					);
				}
			}

			if (alreadyReceived && !postCreateWarning) {
				closeDialog();
				return;
			}

			setSuccessDetail(postCreateWarning ?? DEFAULT_SUCCESS_DETAIL);
			setStep('success');
			scheduleClose();
		} catch (error) {
			console.error('Failed to create library:', error);
			setErrorMessage(
				error instanceof Error
					? error.message
					: 'Failed to create library'
			);
			setStep('error');
			cleanupAsyncState();
		}
	});

	// Creating state
	if (step === 'creating') {
		return (
			<Dialog
				dialog={dialog}
				form={form}
				title="Creating Library"
				icon={<Books size={20} weight="fill" />}
				hideButtons
			>
				<div className="flex flex-col items-center justify-center gap-4 py-8">
					<CircleNotch
						className="text-accent size-12 animate-spin"
						weight="bold"
					/>
					<div className="text-center">
						<p className="text-ink text-sm font-medium">
							Creating your library...
						</p>
						<p className="text-ink-dull mt-1 text-xs">
							This may take a moment
						</p>
					</div>
				</div>
			</Dialog>
		);
	}

	if (step === 'success') {
		return (
			<Dialog
				dialog={dialog}
				form={form}
				title="Library Created"
				icon={<Books size={20} weight="fill" />}
				hideButtons
			>
				<div className="flex flex-col items-center justify-center gap-4 py-8">
					<CheckCircle
						className="size-12 text-green-500"
						weight="fill"
					/>
					<div className="text-center">
						<p className="text-ink text-sm font-medium">
							Library created successfully!
						</p>
						<p className="text-ink-dull mt-1 text-xs">
							{successDetail}
						</p>
					</div>
				</div>
			</Dialog>
		);
	}

	// Error state
	if (step === 'error') {
		return (
			<Dialog
				dialog={dialog}
				form={form}
				title="Error"
				icon={
					<Warning size={20} weight="fill" className="text-red-500" />
				}
				ctaLabel="Try Again"
				onSubmit={async () => {
					setStep('form');
					setErrorMessage(null);
				}}
				onCancelled={true}
			>
				<div className="flex flex-col items-center justify-center gap-4 py-6">
					<Warning className="size-12 text-red-500" weight="fill" />
					<div className="text-center">
						<p className="text-ink text-sm font-medium">
							Failed to create library
						</p>
						<p className="mt-1 text-xs text-red-400">
							{errorMessage}
						</p>
					</div>
				</div>
			</Dialog>
		);
	}

	// Form state (default)
	return (
		<Dialog
			dialog={dialog}
			form={form}
			onSubmit={onSubmit}
			title="Create New Library"
			icon={<Books size={20} weight="fill" />}
			description="A library is a container for your files, tags, and organization"
			ctaLabel="Create Library"
			onCancelled={true}
			loading={createLibrary.isPending}
		>
			<div className="space-y-4">
				<div className="space-y-2">
					<Label slug="name">Library Name</Label>
					<Input
						{...form.register('name', {
							required: 'Name is required'
						})}
						size="md"
						placeholder="My Library"
						autoFocus
						className="bg-app-input"
					/>
					{form.formState.errors.name && (
						<p className="text-xs text-red-500">
							{form.formState.errors.name.message}
						</p>
					)}
				</div>

				<div className="space-y-2">
					<Label>
						Location{' '}
						<span className="text-ink-faint font-normal">
							(optional)
						</span>
					</Label>
					<div className="relative">
						<Input
							value={form.watch('path') || ''}
							onChange={(e) =>
								form.setValue('path', e.target.value || null)
							}
							size="md"
							placeholder="Default location"
							className="bg-app-input pr-12"
						/>
						{platform.openDirectoryPickerDialog && (
							<Button
								type="button"
								variant="gray"
								size="sm"
								onClick={handleBrowse}
								className="absolute right-1.5 top-1/2 -translate-y-1/2"
							>
								<FolderOpen size={16} weight="bold" />
							</Button>
						)}
					</div>
					<p className="text-ink-faint text-xs">
						Leave empty to use the default location
					</p>
				</div>
			</div>
		</Dialog>
	);
}
