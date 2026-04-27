import {
	CaretRight,
	DotsSixVertical,
	PencilSimple,
	Trash
} from '@phosphor-icons/react';
import {useLibraryMutation} from '@sd/ts-client';
import type {SpaceGroup} from '@sd/ts-client';
import clsx from 'clsx';
import {useState} from 'react';
import {useContextMenu} from '../../hooks/useContextMenu';

interface GroupHeaderProps {
	label: string;
	isCollapsed: boolean;
	onToggle: () => void;
	rightComponent?: React.ReactNode;
	actionComponent?: React.ReactNode;
	sortableAttributes?: any;
	sortableListeners?: any;
	group?: SpaceGroup;
	allowCustomization?: boolean;
}

export function GroupHeader({
	label,
	isCollapsed,
	onToggle,
	rightComponent,
	actionComponent,
	sortableAttributes,
	sortableListeners,
	group,
	allowCustomization = false
}: GroupHeaderProps) {
	const hasSortable = sortableAttributes && sortableListeners;
	const [isRenaming, setIsRenaming] = useState(false);
	const [newName, setNewName] = useState(label);

	const updateGroup = useLibraryMutation('spaces.update_group');
	const deleteGroup = useLibraryMutation('spaces.delete_group');

	const handleRename = async () => {
		if (!group || !newName.trim() || newName === label) {
			setIsRenaming(false);
			setNewName(label);
			return;
		}

		try {
			await updateGroup.mutateAsync({
				group_id: group.id,
				name: newName.trim(),
				is_collapsed: null
			});
			setIsRenaming(false);
		} catch (error) {
			console.error('Failed to rename group:', error);
			setNewName(label);
			setIsRenaming(false);
		}
	};

	const handleDelete = async () => {
		if (!group) return;

		try {
			await deleteGroup.mutateAsync({group_id: group.id});
		} catch (error) {
			console.error('Failed to delete group:', error);
		}
	};

	const contextMenu = useContextMenu({
		items: [
			{
				icon: PencilSimple,
				label: 'Rename Group',
				onClick: () => {
					setNewName(label);
					setIsRenaming(true);
				},
				condition: () => allowCustomization
			},
			{type: 'separator'},
			{
				icon: Trash,
				label: 'Delete Group',
				onClick: handleDelete,
				variant: 'danger' as const,
				condition: () => allowCustomization
			}
		]
	});

	const handleContextMenu = async (e: React.MouseEvent) => {
		if (!allowCustomization) return;
		e.preventDefault();
		e.stopPropagation();
		await contextMenu.show(e);
	};

	return (
		<div className="mb-1 flex w-full items-center gap-1 px-1">
			{/* Drag Handle - Only show if sortable */}
			{hasSortable && (
				<div
					{...sortableAttributes}
					{...sortableListeners}
					className="text-sidebar-inkFaint hover:text-sidebar-ink -ml-1 cursor-grab px-0.5 py-2 transition-colors active:cursor-grabbing"
				>
					<DotsSixVertical size={12} weight="bold" />
				</div>
			)}

			{/* Collapsible Button or Rename Input */}
			{isRenaming ? (
				<input
					type="text"
					value={newName}
					onChange={(e) => setNewName(e.target.value)}
					onKeyDown={(e) => {
						if (e.key === 'Enter') {
							handleRename();
						} else if (e.key === 'Escape') {
							setIsRenaming(false);
							setNewName(label);
						}
					}}
					onBlur={handleRename}
					autoFocus
					className="text-tiny bg-sidebar-box border-sidebar-line text-sidebar-ink placeholder:text-sidebar-ink-faint focus:border-accent flex-1 rounded-md border px-2 py-1 font-semibold tracking-wider outline-none"
				/>
			) : (
				<>
					<button
						type="button"
						onClick={onToggle}
						onContextMenu={handleContextMenu}
						className="text-tiny text-sidebar-ink-faint hover:text-sidebar-ink flex min-h-[32px] flex-1 items-center gap-2 py-2 font-semibold tracking-wider opacity-60 transition-colors"
					>
						<CaretRight
							className={clsx(
								'transition-transform',
								!isCollapsed && 'rotate-90'
							)}
							size={10}
							weight="bold"
						/>
						<span>{label}</span>
						{rightComponent}
					</button>
					{actionComponent}
				</>
			)}
		</div>
	);
}
