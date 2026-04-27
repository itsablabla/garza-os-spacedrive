import {Database, Plus} from '@phosphor-icons/react';
import {useLocation, useNavigate} from 'react-router-dom';
import {useLibraryQuery} from '../../contexts/SpacedriveContext';
import {useAdapterIcons} from '../../hooks/useAdapterIcons';
import {GroupHeader} from './GroupHeader';

interface SourcesGroupProps {
	isCollapsed: boolean;
	onToggle: () => void;
	sortableAttributes?: any;
	sortableListeners?: any;
}

export function SourcesGroup({
	isCollapsed,
	onToggle,
	sortableAttributes,
	sortableListeners
}: SourcesGroupProps) {
	const navigate = useNavigate();
	const location = useLocation();
	const {getIcon} = useAdapterIcons();

	const {data: sources} = useLibraryQuery({
		type: 'sources.list',
		input: {data_type: null}
	});

	const sourcesList = sources ?? [];
	const isSourcesHome = location.pathname === '/sources';
	const isAdaptersView = location.pathname === '/sources/adapters';

	return (
		<div>
			<GroupHeader
				label="Sources"
				isCollapsed={isCollapsed}
				onToggle={onToggle}
				sortableAttributes={sortableAttributes}
				sortableListeners={sortableListeners}
				actionComponent={
					<button
						type="button"
						onClick={() => navigate('/sources/adapters')}
						className="text-sidebar-ink-faint hover:bg-sidebar-selected/30 hover:text-sidebar-ink rounded-md p-1 transition-colors"
						aria-label="Add source"
						title="Add source"
					>
						<Plus size={12} weight="bold" />
					</button>
				}
			/>

			{!isCollapsed && (
				<div className="space-y-0.5">
					<button
						onClick={() => navigate('/sources')}
						className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm font-medium ${
							isSourcesHome
								? 'bg-sidebar-selected/30 text-sidebar-ink'
								: 'text-sidebar-inkDull hover:bg-sidebar-selected/20 hover:text-sidebar-ink'
						}`}
					>
						<Database
							className="size-4 shrink-0"
							weight={isSourcesHome ? 'fill' : 'bold'}
						/>
						<span className="flex-1 truncate">All Sources</span>
					</button>
					<button
						onClick={() => navigate('/sources/adapters')}
						className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm font-medium ${
							isAdaptersView
								? 'bg-sidebar-selected/30 text-sidebar-ink'
								: 'text-sidebar-inkDull hover:bg-sidebar-selected/20 hover:text-sidebar-ink'
						}`}
					>
						<Plus
							className="size-4 shrink-0"
							weight={isAdaptersView ? 'fill' : 'bold'}
						/>
						<span className="flex-1 truncate">Add Source</span>
					</button>
					{sourcesList.map((source) => {
						const isActive =
							location.pathname === `/sources/${source.id}`;
						const iconSvg = getIcon(source.adapter_id);

						return (
							<button
								key={source.id}
								onClick={() =>
									navigate(`/sources/${source.id}`)
								}
								className={`flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm font-medium ${
									isActive
										? 'bg-sidebar-selected/30 text-sidebar-ink'
										: 'text-sidebar-inkDull hover:bg-sidebar-selected/20 hover:text-sidebar-ink'
								}`}
							>
								{iconSvg ? (
									<div
										className={`size-4 shrink-0 [&>svg]:h-full [&>svg]:w-full ${
											isActive
												? 'opacity-100'
												: 'opacity-60 grayscale'
										}`}
										dangerouslySetInnerHTML={{
											__html: iconSvg
										}}
									/>
								) : (
									<Database
										className="size-4 shrink-0"
										weight={isActive ? 'fill' : 'bold'}
									/>
								)}
								<span className="truncate">{source.name}</span>
							</button>
						);
					})}
				</div>
			)}
		</div>
	);
}
